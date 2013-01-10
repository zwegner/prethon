################################################################################
## 
## Prethon-Python-based preprocessor.
## 
## Copyright 2013 Zach Wegner
##
## This file is part of Prethon.
## 
## Prethon is free software: you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation, either version 3 of the License, or
## (at your option) any later version.
## 
## Prethon is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
## 
## You should have received a copy of the GNU General Public License
## along with Prethon.  If not, see <http://www.gnu.org/licenses/>.
## 
################################################################################

import copy
import io
import os
import re
import subprocess
import sys

# This state is used by the preprocessor external functions. The preprocessor
# uses its own local state for the parsing, but the preprocessed code needs
# access (through this module) to this state.
pre_state = None

# Mode enum
NORMAL, PRE, EXPR, QUOTE_H, QUOTE_CONT, QUOTE = range(6)

# Options
depend = None
depend_files = []
output_line_nos = False

################################################################################
## Preprocessor functions ######################################################
################################################################################

# Emit function. This is what preprocessor code uses to emit real code.
def emit(s):
    global pre_state
    pre_state.out.write(str(s))

# Include: Recursively call the preprocessor
def include(path, var_dict=None, mode=NORMAL, output=None):
    global pre_state, depend_files
    depend_files += [path]
    if var_dict:
        vd = pre_state.variables.copy()
        for key, value in var_dict.items():
            vd[key] = value
        pre_state.variables = vd
    if output is None:
        output = pre_state.out
    pre(output, pre_state.pre_globals, path, mode=mode)

def include_py(path, var_dict=None):
    include(path, var_dict, mode=PRE)

################################################################################
## Parser functions ############################################################
################################################################################

PRE_START = '<@'
PRE_END = '@>'
EXPR_START = '<$'
EXPR_END = '$>'
QUOTE_H_START = '<#'
QUOTE_H_END = ':'
QUOTE_CONT_START = '##'
QUOTE_CONT_END = '\n'
QUOTE_END = '#>'

DELIMS = [PRE_START, PRE_END, EXPR_START, EXPR_END, QUOTE_H_START, QUOTE_H_END,
    QUOTE_CONT_START, QUOTE_CONT_END, QUOTE_END]

# Make the reentrant
class ParserState:
    def __init__(self, mode, file, out):
        self.cur_block = []
        self.quote_blocks = []
        self.indent = 0
        self.mode = []
        self.last_mode = -1
        self.last_len = -1
        self.quote = False
        self.last_quote = False
        self.emit = [True]
        self.path = file
        self.out = out
        self.lineno = 1
        self.push(mode)

    def push(self, mode):
        # Flush anything from the last mode
        if len(self.mode) >= 1:
            self.flush(self.mode[-1])

        self.mode.append(mode)

        self.cur_block.append([])
        if mode == QUOTE_H:
            self.quote_blocks.append([])

    def pop(self):
        mode = self.mode.pop()
        if mode == QUOTE:
            s = self.quote_fn(self.quote_blocks.pop())
            self.run(s)
        else:
            self.flush(mode)

        self.cur_block.pop()

    def flush(self, mode):
        global output_line_nos
        block = ''.join(self.cur_block.pop())
        self.cur_block.append([])
        s = ''
        if block:
            if mode == NORMAL:
                s = 'emit(%s)\n' % repr(block)
            elif mode == PRE:
                s = block
            elif mode == EXPR:
                s = 'emit(%s)\n' % block
            elif mode == QUOTE_H or mode == QUOTE_CONT:
                self.quote_blocks[-1].append(block)
                s = ''

        s = self.fix_ws(s)
        self.run(s)
        if output_line_nos:
            s = 'emit("\\n#line %s\\n")\n' % self.lineno
            self.run(s)

    def run(self, s):
        # Execute the python code
        if QUOTE in self.mode:
            self.quote_blocks[-1].append(s)
        elif s is not '':
            try:
                exec(s, self.pre_globals)
            except:
                print('Exception in code:\n%s' % s)
                raise

    def quote_fn(self, blocks):
        header = blocks[0]
        body = ''.join(blocks[1:])

        header += ':'
        header = self.fix_ws(header)

        # Set up body
        self.indent += 4
        body = self.fix_ws(body)
        self.indent -= 4

        return '\n'.join([header, body])

    # Fix the indenting of a block to be at the global scope
    def fix_ws(self, block):
        lines = block.split('\n')

        pre = None
        l = 0
        for line in lines:
            if not line.strip():
                continue
            elif pre is None:
                pre = re.match('\\s*', line).group(0)
                l = len(pre)
            else:
                for x in range(l):
                    if x >= len(line) or line[x] != pre[x]:
                        l = x
                        break

        # Re-indent the lines to match the indent level
        lines = [line[l:] if line.strip() else line for line in lines]
        lines = [' '*self.indent + line for line in lines]

        return '%s\n' % '\n'.join(lines)

# Just add a character to a buffer
def _emit(state, s):
    state.cur_block[-1] += [s]
    if state.mode[-1] == QUOTE and s:
        s = 'emit(%s)\n' % repr(s)
        state.quote_blocks[-1].append(s)

def tokenize(s, delims):
    while s:
        idx = None
        t = None
        for d in delims:
            i = s.find(d)
            if i != -1 and (idx is None or i < idx):
                idx = i
                t = d

        if t:
            yield s[:idx]
            yield t
            s = s[idx + len(t):]
        else:
            yield s
            s = ''

def pre(out, pre_globals, file, mode=NORMAL):
    global pre_state

    # Set up the state of the parser
    state = ParserState(mode, file, out)

    # Set up globals for the pre-space
    state.pre_globals = pre_globals

    # Set the global state so functions in this module can use it while being
    # called from the preprocessed code. We back up the old state since we can
    # preprocess recursively (through includes)
    old_state = pre_state
    pre_state = state

    # Open the file for reading
    with open(file, 'rt') as f:
        for c in f:
            for tok in tokenize(c, DELIMS):
                state.lineno += tok.count('\n')
                # Regular preprocessed sections
                if tok == PRE_START:
                    state.push(PRE)
                elif tok == PRE_END:
                    state.pop()
                # Def
                elif tok == EXPR_START:
                    state.push(EXPR)
                elif tok == EXPR_END:
                    state.pop()
                # Quote
                elif tok == QUOTE_H_START:
                    state.push(QUOTE_H)
                elif tok == QUOTE_H_END and state.mode[-1] == QUOTE_H:
                    state.pop()
                    state.push(QUOTE)
                elif tok == QUOTE_CONT_START and state.mode[-1] == QUOTE:
                    state.push(QUOTE_CONT)
                elif tok == QUOTE_CONT_END and state.mode[-1] == QUOTE_CONT:
                    state.pop()
                elif tok == QUOTE_END:
                    state.pop()
                else:
                    _emit(state, tok)

    # Finish up: flush the last block of characters
    state.pop()

    # Restore the old parser state
    pre_state = old_state

# Wrapper class for passing stuff to the program
class PreData:
    pass

def usage(name):
    print('Usage: %s [options] <input> <output> [var=value...]' % name)
    sys.exit(1)

def main(args):
    global depend, depend_files, output_line_nos

    # Set up options
    if len(args) < 3:
        usage(args[0])

    while True:
        if args[1] == '-d':
            depend = args[2]
            args[1:] = args[3:]
        elif args[1] == '-l':
            output_line_nos = True
            args[1:] = args[2:]
        else:
            break

    # Set up input/output files
    i = args[1]
    o = args[2]

    # Loop over all key=value pairs and set these variables.
    variables = {}
    for opt in sys.argv[3:]:
        key, _, value = opt.partition('=')
        variables[key] = value

    p = PreData()
    p.variables = variables

    # Preprocessor globals. This keeps the state of the preprocessed blocks
    pre_globals = {
        'emit' : emit,
        'include' : include,
        'include_py' : include_py,
        'pre' : p
    }

    # Run the preprocessor
    with open(o, 'wt') as out:
        pre(out, pre_globals, i)

    if depend:
        with open(depend, 'wt') as d_file:
            d_file.write('%s: %s\n' % (o, ' '.join(depend_files)))

if __name__ == '__main__':
    main(sys.argv)
