#!/usr/bin/python
# coding: utf-8

# The MIT License (MIT)
# Copyright (c) 2013 Gaspard Jankowiak
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import curses
from time import sleep, time
import argparse
import sys
import os
import shelve
import signal
import shlex
from subprocess import call, STDOUT, Popen, PIPE
import select
import struct
from fcntl import ioctl
import termios
import imp

rc = imp.new_module('rc')

ID_FIELD_WIDTH   = 6
NAME_FIELD_WIDTH = 22
RES_FIELD_WIDTH  = 12
VIEWS_FIELD_WIDTH = 7
PLAYING_FIELD_OFFSET = ID_FIELD_WIDTH + NAME_FIELD_WIDTH + RES_FIELD_WIDTH + VIEWS_FIELD_WIDTH + 6

PROG_STRING    = 'livestreamer-curses'
VERSION_STRING = '1.0.1'
TITLE_STRING   = '{0} v{1}'.format(PROG_STRING, VERSION_STRING)

DEFAULT_RESOLUTION_HARD = '480p'

class QueueFull(Exception): pass
class QueueDuplicate(Exception): pass

class ProcessList(object):
    """ Small class to store and handle calls to a given callable """

    def __init__(self, f, max_size=10):
        """ Create a ProcessList

        f        : callable for which a process will be spawned for each call to put
        max_size : the maximum size of the ProcessList

        """
        self.q        = {}
        self.max_size = max_size
        self.call     = f

    def __del__(self):
        self.terminate()

    def full(self):
        """ Check is the List is full, returns a bool """
        return len(self.q) == self.max_size

    def empty(self):
        """ Check is the List is full, returns a bool """
        return len(self.q) == 0

    def put(self, id, *args):
        """ Spawn a new background process

        id   : int, id of the process,
               unique among the queue or will raise QueueDuplicate
        args : optional arguments to pass to the callable

        """

        if len(self.q) < self.max_size:
            if self.q.has_key(id):
                raise QueueDuplicate
            p = self.call(*args)
            self.q[id] = p
        else:
            raise QueueFull

    def get_finished(self):
        """ Clean up terminated processes and returns the list of their ids """
        indices  = []
        for id, v in self.q.items():
            if v.poll() != None:
                indices.append(id)

        for i in indices:
            self.q.pop(i)
        return indices

    def get_process(self, id):
        """ Get a process by id, returns None if there is no match """
        return self.q.get(id)

    def get_stdouts(self):
        """ Get the list of stdout of each process """
        souts = []
        for v in self.q.values():
            souts.append(v.stdout)
        return souts

    def terminate_process(self, id):
        """ Terminate a process by id """
        try:
            p = self.q.pop(id)
            p.terminate()
            return p
        except:
            return None

    def terminate(self):
        """ Terminate all processes """
        for w in self.q.values():
            try:
                w.terminate()
            except:
                pass

        self.q = {}

class StreamPlayer(object):
    """ Provides a callable to play a given url """

    def play(self, url, res, cmd=['livestreamer']):
        full_cmd = list(cmd)
        full_cmd.extend([url, res])
        return Popen(full_cmd, stdout=PIPE, stderr=STDOUT)

class StreamList(object):

    def __init__(self, filename):
        """ Init and try to load a stream list, nothing about curses yet """

        # Open the storage (create it if necessary
        f = shelve.open(filename, 'c')
        self.max_id = 0

        # Sort streams by view count
        if f.has_key('streams'):
            self.streams = sorted(f['streams'], key=lambda s:s['seen'], reverse=True)
            for s in self.streams:
                # Max id, needed when adding a new stream
                self.max_id = max(self.max_id, s['id'])
        else:
            self.streams = []
        self.filtered_streams = list(self.streams)
        self.filter = ''

        if 'LIVESTREAMER_COMMANDS' in dir(rc):
            self.cmd_list = map(shlex.split, rc.LIVESTREAMER_COMMANDS)
        else:
            self.cmd_list = [['livestreamer']]
        self.cmd_index = 0
        self.cmd = self.cmd_list[self.cmd_index]

        if 'DEFAULT_RESOLUTION' in dir(rc):
            self.default_res = rc.DEFAULT_RESOLUTION
        else:
            self.default_res = DEFAULT_RESOLUTION_HARD

        self.store = f
        self.store.sync()

        self.no_streams = self.streams == []
        self.no_stream_shown = self.no_streams
        self.q = ProcessList(StreamPlayer().play)

    def __del__(self):
        """ Stop playing streams and sync storage """
        self.q.terminate()
        self.store['cmd'] = self.cmd
        self.store['streams'] = self.streams
        self.store.close()

    def __call__(self, s):
        # Terminal initialization
        self.init(s)
        # Main event loop
        self.run()

    def init(self, s):
        """ Initialize the text interface """

        # Hide cursor
        curses.curs_set(0)

        self.s = s

        self.get_screen_size()

        self.pads = {}
        self.offsets = {}

        self.init_help()
        self.init_streams_pad()
        self.current_pad = 'streams'

        self.set_title(TITLE_STRING)
        self.set_footer('Ready')

        self.got_g = False
        self.status = ''

        signal.signal(28, self.resize)

    def getheightwidth(self):
        """ getwidth() -> (int, int)

        Return the height and width of the console in characters
        https://groups.google.com/forum/#!msg/comp.lang.python/CpUszNNXUQM/QADpl11Z-nAJ"""
        try:
            return int(os.environ["LINES"]), int(os.environ["COLUMNS"])
        except KeyError:
            height, width = struct.unpack(
                "hhhh", ioctl(0, termios.TIOCGWINSZ ,"\000"*8))[0:2]
            if not height: return 25, 80
            return height, width

    def resize(self, signum, obj):
        """ handler for SIGWINCH """
        self.s.clear()
        self.s.refresh()
        stream_cursor = self.pads['streams'].getyx()[0]
        for pad in self.pads.values():
            pad.clear()
        self.pads = {}
        self.get_screen_size()
        self.s.resize(self.max_y+1, self.max_x+1)
        self.set_title(TITLE_STRING)
        self.s.refresh()
        self.init_help()
        self.init_streams_pad()
        self.move(stream_cursor, absolute=True, pad_name='streams')
        self.show()

    def run(self):
        """ Main event loop """

        # Show stream list
        self.show_streams()

        while True:
            self.s.refresh()

            # See if any stream has ended
            self.check_stopped_streams()

            # Wait on stdin or on the streams output
            souts = self.q.get_stdouts()
            souts.append(sys.stdin)
            try:
                (r, w, x) = select.select(souts, [], [], 1)
            except select.error:
                continue
            for fd in r:
                if fd != sys.stdin:
                    # Set the new status line only if non-empty
                    msg = fd.readline()
                    if len(msg) > 0:
                        self.status = msg[:-1]
                        self.redraw_status()
                else:
                    # Main event loop
                    c = self.pads[self.current_pad].getch()
                    if c == curses.KEY_UP or c == ord('k') or c == 65:
                        self.move(-1)
                    elif c == curses.KEY_DOWN or c == ord('j') or c == 66:
                        self.move(1)
                    elif c == ord('f'):
                        if self.current_pad == 'streams':
                            self.filter_streams()
                    elif c == ord('F'):
                        if self.current_pad == 'streams':
                            self.clear_filter()
                    elif c == ord('g'):
                        if self.got_g:
                            self.move(0, absolute=True)
                            self.got_g = False
                            continue
                        self.got_g = True
                    elif c == ord('G'):
                        self.move(len(self.filtered_streams)-1, absolute=True)
                    elif c == ord('q'):
                        if self.current_pad == 'streams':
                            self.q.terminate()
                            return
                        else:
                            self.show_streams()
                    elif c == 27: # ESC
                        if self.current_pad != 'streams':
                            self.show_streams()
                    if self.current_pad == 'help':
                        continue
                    elif c == 10:
                        self.play_stream()
                    elif c == ord('s'):
                        self.stop_stream()
                    elif c == ord('c'):
                        self.reset_stream()
                    elif c == ord('n'):
                        self.edit_stream('name')
                    elif c == ord('r'):
                        self.edit_stream('res')
                    elif c == ord('u'):
                        self.edit_stream('url')
                    elif c == ord('l'):
                        self.show_commandline()
                    elif c == ord('L'):
                        self.shift_commandline()
                    elif c == ord('a'):
                        self.prompt_new_stream()
                    elif c == ord('d'):
                        self.delete_stream()
                    elif c == ord('h') or c == ord('?'):
                        self.show_help()

    def get_screen_size(self):
        """ Setup screen size and padding

        We have need 2 free lines at the top and 2 free lines at the bottom

        """
        max_y, max_x = self.getheightwidth()
        #raise ValueError('{} {}'.format(max_y, max_x))
        self.pad_x = 0
        self.max_y, self.max_x = (max_y-1, max_x-1)
        self.pad_w = max_x-1*self.pad_x
        self.pad_h = max_y-3

    def overwrite_line(self, msg, attr=curses.A_NORMAL):
        self.s.clrtoeol()
        self.s.addstr(msg, attr)
        self.s.chgat(attr)

    def set_title(self, msg):
        """ Set first header line text """
        self.s.move(0, 0)
        self.overwrite_line(msg, curses.A_REVERSE)

    def set_header(self, msg):
        """ Set second head line text """
        self.s.move(1, 0)
        self.overwrite_line(msg, attr=curses.A_NORMAL)

    def set_footer(self, msg):
        """ Set first footer line text """
        self.s.move(self.max_y-1, 0)
        self.overwrite_line(msg, attr=curses.A_REVERSE)

    def init_help(self):
        help_pad_length = 25    # there should be a neater way to do this
        h = curses.newpad(help_pad_length, self.pad_w)

        h.addstr( 0, 0, 'STREAM MANAGEMENT', curses.A_BOLD)
        h.addstr( 2, 0, '  Enter : start stream')
        h.addstr( 3, 0, '  s     : stop stream')
        h.addstr( 4, 0, '  r     : change stream resolution')
        h.addstr( 5, 0, '  n     : change stream name')
        h.addstr( 6, 0, '  u     : change stream URL')
        h.addstr( 7, 0, '  c     : reset stream view count')
        h.addstr( 8, 0, '  a     : add stream')
        h.addstr( 9, 0, '  d     : delete stream')

        h.addstr(11, 0, '  l     : show command line')
        h.addstr(12, 0, '  L     : cycle command line')

        h.addstr(15, 0, 'NAVIGATION', curses.A_BOLD)
        h.addstr(17, 0, '  j/up  : up one line')
        h.addstr(18, 0, '  k/down: down one line')
        h.addstr(19, 0, '  f     : filter streams')
        h.addstr(20, 0, '  F     : clear filter')
        h.addstr(21, 0, '  gg    : go to top')
        h.addstr(22, 0, '  G     : go to bottom')
        h.addstr(23, 0, '  h/?   : show this help')
        h.addstr(24, 0, '  q     : quit')

        self.pads['help'] = h
        self.offsets['help'] = 0

    def show(self):
        funcs = {
            'streams' : self.show_streams,
            'help'    : self.show_help
        }
        funcs[self.current_pad]()

    def show_help(self):
        """ Redraw Help screen and wait for any input to leave """
        self.s.move(1,0)
        self.s.clrtobot()
        self.set_header('Help'.center(self.pad_w))
        self.set_footer(' ESC or \'q\' to return to main menu')
        self.s.refresh()
        self.current_pad = 'help'
        self.refresh_current_pad()

    def init_streams_pad(self, start_row=0):
        """ Create a curses pad and populate it with a line by stream """
        y = 0
        if self.pads.get('streams'):
            self.pads['streams'].clear()
            self.refresh_current_pad()
        pad = curses.newpad(max(1,len(self.filtered_streams)), self.pad_w)
        for s in self.filtered_streams:
            pad.addstr(y, 0, self.format_stream_line(s))
            y+=1
        self.offsets['streams'] = 0
        pad.move(start_row, 0)
        if not self.no_streams:
            pad.chgat(curses.A_REVERSE)
        self.pads['streams'] = pad

    def show_streams(self):
        self.s.move(1,0)
        self.s.clrtobot()
        self.current_pad = 'streams'
        if not self.no_streams:
            id = 'ID'.center(ID_FIELD_WIDTH)
            name = 'Name'.center(NAME_FIELD_WIDTH)
            res = 'Resolution'.center(RES_FIELD_WIDTH)
            views = 'Views'.center(VIEWS_FIELD_WIDTH)
            self.set_header('{}|{}|{}|{}| Status'.format(id, name, res, views))
            self.redraw_stream_footer()
            self.redraw_status()
        else:
            self.s.addstr(5, 5, 'It seems you don\'t have any stream yet,')
            self.s.addstr(6, 5, 'hit \'a\' to add a new one.')
            self.s.addstr(8, 5, 'Hit \'?\' for help.')
            self.set_footer(' Ready')
        self.s.refresh()
        self.refresh_current_pad()

    def refresh_current_pad(self):
        pad = self.pads[self.current_pad]
        pad.refresh(self.offsets[self.current_pad], 0, 2, self.pad_x, self.pad_h, self.pad_w)

    def move(self, direction, absolute=False, pad_name=None):
        """ Scroll the current pad

        direction : (int)  move by one in the given direction
                           -1 is up, 1 is down. If absolute is True,
                           go to position direction.
                           Behaviour is affected by cursor_line and scroll_only below
        absolute  : (bool)
        """

        # pad in this lists have the current line highlighted
        cursor_line = [ 'streams' ]

        # pads in this list will be moved screen-wise as opposed to line-wise
        # if absolute is set, will go all the way top or all the way down depending
        # on direction
        scroll_only = [ 'help' ]

        if not pad_name:
            pad_name = self.current_pad
        pad = self.pads[pad_name]
        if pad_name == 'streams' and self.no_streams:
            return
        (row, col) = pad.getyx()
        new_row    = row
        offset = self.offsets[pad_name]
        new_offset = offset
        if pad_name in scroll_only:
            if absolute:
                if direction > 0:
                    new_offset = pad.getmaxyx()[0] - self.pad_h + 1
                else:
                    new_offset = 0
            else:
                if direction > 0:
                    new_offset = min(pad.getmaxyx()[0] - self.pad_h + 1, offset + self.pad_h)
                elif offset > 0:
                    new_offset = max(0, offset - self.pad_h)
        else:
            if absolute and direction >= 0 and direction < pad.getmaxyx()[0]:
                if direction < offset:
                    new_offset = direction
                elif direction > offset + self.pad_h - 2:
                    new_offset = direction - self.pad_h + 2
                new_row = direction
            else:
                if direction == -1 and row > 0:
                    if row == offset:
                        new_offset -= 1
                    new_row = row-1
                elif direction == 1 and row < len(self.filtered_streams)-1:
                    if row == offset + self.pad_h - 2:
                        new_offset += 1
                    new_row = row+1
        if pad_name in cursor_line:
            pad.move(row, 0)
            pad.chgat(curses.A_NORMAL)
        self.offsets[pad_name] = new_offset
        pad.move(new_row, 0)
        if pad_name in cursor_line:
            pad.chgat(curses.A_REVERSE)
        if pad_name == 'streams':
            self.redraw_stream_footer()
        self.refresh_current_pad()
        self.redraw_stream_footer()

    def format_stream_line(self, stream):
        id = '{} '.format(stream['id']).rjust(ID_FIELD_WIDTH)
        name = ' {}'.format(stream['name']).ljust(NAME_FIELD_WIDTH)
        res  = ' {}'.format(stream['res']).ljust(RES_FIELD_WIDTH)
        views  = '{} '.format(stream['seen']).rjust(VIEWS_FIELD_WIDTH)
        p = self.q.get_process(stream['id']) != None
        if p:
            indicator = '>'
        else:
            indicator = ' '
        return '{}|{}|{}|{}|  {}'.format(id, name, res, views, indicator)

    def redraw_current_line(self):
        """ Redraw the highlighted line """
        if self.no_streams:
            return
        row = self.pads[self.current_pad].getyx()[0]
        s = self.filtered_streams[row]
        pad = self.pads['streams']
        pad.move(row, 0)
        pad.clrtoeol()
        pad.addstr(row, 0, self.format_stream_line(s), curses.A_REVERSE)
        pad.chgat(curses.A_REVERSE)
        pad.move(row, 0)
        self.refresh_current_pad()

    def redraw_status(self):
        self.s.move(self.max_y, 0)
        self.overwrite_line(self.status[:self.max_x], curses.A_NORMAL)

    def redraw_stream_footer(self):
        if not self.no_stream_shown:
            row = self.pads[self.current_pad].getyx()[0]
            s = self.filtered_streams[row]
            self.set_footer('{}/{} {} {}'.format(row+1, len(self.filtered_streams), s['url'], s['res']))
            self.s.refresh

    def check_stopped_streams(self):
        finished = self.q.get_finished()
        for f in finished:
            for s in self.streams:
                try:
                    i = self.filtered_streams.index(s)
                    if f == s['id']:
                        self.set_footer('Stream {} has stopped'.format(s['name']))
                        if i == self.pads[self.current_pad].getyx()[0]:
                            attr = curses.A_REVERSE
                        else:
                            attr = curses.A_NORMAL
                        self.pads['streams'].addch(i, PLAYING_FIELD_OFFSET, ' ', attr)
                        self.refresh_current_pad()
                except:
                    pass

    def prompt_input(self, prompt=''):
        self.s.move(self.max_y-1, 0)
        self.s.clrtoeol()
        self.s.addstr(prompt)
        curses.curs_set(1)
        curses.echo()
        r = self.s.getstr()
        curses.noecho()
        curses.curs_set(0)
        self.s.move(self.max_y-1, 0)
        self.s.clrtoeol()
        return r

    def prompt_confirmation(self, prompt='', def_yes=False):
        self.s.move(self.max_y-1, 0)
        self.s.clrtoeol()
        if def_yes:
            hint = '[y]/n'
        else:
            hint = 'y/[n]'
        self.s.addstr('{} {} '.format(prompt, hint))
        curses.curs_set(1)
        curses.echo()
        r = self.s.getch()
        curses.noecho()
        curses.curs_set(0)
        self.s.move(self.max_y-1, 0)
        self.s.clrtoeol()
        if r == ord('y'):
            return True
        elif r == ord('n'):
            return False
        else:
            return def_yes

    def sync_store(self):
        self.store['streams'] = self.streams
        self.store.sync()

    def bump_stream(self, stream, throttle=False):
        t = int(time())

        # only bump if stream was last started some time ago
        if throttle and  t - stream['last_seen'] < 60*1:
            return
        stream['seen'] += 1
        stream['last_seen'] = t
        self.sync_store()

    def find_stream(self, sel, key='id'):
        for s in self.streams:
            if s[key] == sel:
                return s
        return None

    def clear_filter(self):
        self.filter = None
        self.filtered_streams = self.streams
        self.no_stream_shown = self.no_streams
        self.status = 'Filter cleared'
        self.init_streams_pad()
        self.refresh_current_pad()
        self.redraw_stream_footer()
        self.redraw_status()

    def filter_streams(self):
        self.filter = self.prompt_input('Filter: ').lower()
        self.filtered_streams = []
        for s in self.streams:
            if self.filter in s['name'].lower() or self.filter in s['url'].lower():
                self.filtered_streams.append(s)
        self.filtered_streams.sort(key=lambda s:s['seen'], reverse=True)
        self.no_stream_shown = len(self.filtered_streams) == 0
        self.status = 'New filter: {0} ({1} matches)'.format(self.filter, len(self.filtered_streams))
        self.init_streams_pad()
        self.refresh_current_pad()
        self.redraw_stream_footer()
        self.redraw_status()

    def add_stream(self, name, url, res=None, bump=False):
        ex_stream = self.find_stream(url, key='url')
        if ex_stream:
            if bump:
                self.bump_stream(ex_stream)
        else:
            if bump:
                seen = 1
                last_seen = int(time())
            else:
                seen = last_seen = 0
            if len(self.streams) == 0:
                id = 1
            else:
                self.max_id += 1
                id = self.max_id

            s_res = res or self.default_res

            if type(s_res) == str:
                actual_res = s_res
            elif type(s_res) == dict:
                actual_res = DEFAULT_RESOLUTION_HARD
                for (k,v) in s_res.iteritems():
                    if k in url:
                        actual_res = v
                        break
            elif callable(s_res):
                actual_res = s_res(url) or DEFAULT_RESOLUTION_HARD
            else:
                actual_res = DEFAULT_RESOLUTION_HARD

            new_stream = {
                    'id'        : id,
                    'name'      : name,
                    'seen'      : seen,
                    'last_seen' : last_seen,
                    'res'       : actual_res,
                    'url'       : url
                }
            self.streams.append(new_stream)
            self.no_streams = False
            if self.filter in name.lower() or self.filter in url.lower():
                self.filtered_streams.append(new_stream)
            self.no_stream_shown = len(self.filtered_streams) == 0
            try: self.init_streams_pad()
            except: pass
            self.sync_store()

    def delete_stream(self):
        if self.no_streams:
            return
        pad = self.pads[self.current_pad]
        s = self.filtered_streams[pad.getyx()[0]]
        if not self.prompt_confirmation('Delete stream {}?'.format(s['name'])):
            return
        self.filtered_streams.remove(s)
        self.streams.remove(s)
        pad.deleteln()
        self.sync_store()
        if len(self.streams) == 0:
            self.no_streams = True
        if len(self.filtered_streams) == 0:
            self.no_stream_shown = True
        if pad.getyx()[0] == len(self.filtered_streams) and not self.no_stream_shown:
            self.move(-1)
            pad.chgat(curses.A_REVERSE)
        self.redraw_current_line()
        self.show_streams()

    def reset_stream(self):
        if self.no_stream_shown:
            return
        pad = self.pads[self.current_pad]
        s = self.filtered_streams[pad.getyx()[0]]
        if not self.prompt_confirmation('Reset stream {}?'.format(s['name'])):
            return
        s['seen']      = 0
        s['last_seen'] = 0
        self.redraw_current_line()
        self.sync_store()

    def edit_stream(self, attr):
        prompt_info = {
                'name'      : 'Name',
                'url'       : 'URL',
                'res'       : 'Resolution'
                }
        if self.no_streams:
            return
        pad = self.pads[self.current_pad]
        s = self.filtered_streams[pad.getyx()[0]]
        new_val = self.prompt_input('{} (empty to cancel): '.format(prompt_info[attr]))
        if new_val != '':
            s[attr] = new_val
            self.redraw_current_line()
        self.redraw_status()
        self.redraw_stream_footer()

    def show_commandline(self):
        self.set_footer('{0}/{1} {2}'.format(self.cmd_index+1, len(self.cmd_list), ' '.join(self.cmd)))

    def shift_commandline(self):
        self.cmd_index += 1
        if self.cmd_index == len(self.cmd_list):
            self.cmd_index = 0
        self.cmd = self.cmd_list[self.cmd_index]
        self.show_commandline()

    def prompt_new_stream(self):
        url = self.prompt_input('New stream URL (empty to cancel): ')
        name = url.split('/')[-1]
        if len(name) > 0:
            self.add_stream(name, url)
            self.move(len(self.filtered_streams)-1, absolute=True)
            self.show_streams()

    def play_stream(self):
        if self.no_stream_shown:
            return
        pad = self.pads[self.current_pad]
        s = self.filtered_streams[pad.getyx()[0]]
        try:
            self.q.put(s['id'], s['url'], s['res'], self.cmd)
            self.bump_stream(s, throttle=True)
            self.redraw_current_line()
            self.refresh_current_pad()
        except Exception as e:
            if type(e) == QueueDuplicate:
                self.set_footer('This stream is already playing')
            elif type(e) == OSError:
                self.set_footer('/!\ Faulty command line: {}'.format(e.strerror))
            else:
                raise e

    def stop_stream(self):
        if self.no_stream_shown:
            return
        pad = self.pads[self.current_pad]
        s = self.filtered_streams[pad.getyx()[0]]
        p = self.q.terminate_process(s['id'])
        if p:
            self.redraw_current_line()
            self.redraw_stream_footer()
            self.redraw_status()

def main():
    parser = argparse.ArgumentParser(description='Livestreamer curses frontend.')
    parser.add_argument('-d', type=unicode, metavar='database', help='default: ~/.livestreamer-curses.db',
                        default=os.path.join(os.environ['HOME'], u'.livestreamer-curses.db'))
    parser.add_argument('-f', type=unicode, metavar='configfile', help='default: ~/.livestreamer-cursesrc',
                        default=os.path.join(os.environ['HOME'], u'.livestreamer-cursesrc'))
    args = parser.parse_args()

    rc_filename = args.f
    if os.path.exists(rc_filename):
        try:
            rc = imp.load_source('rc', rc_filename)
        except Exception as e:
            sys.stderr.write('Failed to read rc file, error was:\n{0}\n'.format(str(e)))
            sys.exit(1)
    l = StreamList(args.d)
    curses.wrapper(l)

if __name__ == '__main__':
    main()