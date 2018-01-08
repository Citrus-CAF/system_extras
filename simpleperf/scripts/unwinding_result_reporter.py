#!/usr/bin/env python
#
# Copyright (C) 2017 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""unwinding_result_reporter.py: report dwarf unwinding results.
   It can be used on perf.data generated with the '-g --log debug' option.
   It is used to help findinig problems of unwinding different libraries
   using libBacktraceOffline.

"""

from __future__ import print_function
import argparse
import bisect
import collections
import copy
import re
import subprocess

from utils import *


class MapEntry(object):

    def __init__(self, start, end, filename):
        self.start = start
        self.end = end
        self.filename = filename

    def __lt__(self, other):
        return self.start < other.start

class ProcessMaps(object):

    def __init__(self):
        self.process_maps = {}  # map from pid to a sorted list of MapEntry.

    def add(self, pid, map_entry):
        old_list = self.process_maps.get(pid, [])
        new_list = []
        map_entry_used = False
        for entry in old_list:
            if entry.end <= map_entry.start:
                new_list.append(entry)
            elif entry.start < map_entry.start:
                entry.end = map_entry.start
                new_list.append(entry)
            else:
                if not map_entry_used:
                    new_list.append(map_entry)
                    map_entry_used = True
                if entry.start >= map_entry.end:
                    new_list.append(entry)
                elif entry.end > map_entry.end:
                    entry.start = map_entry.end
                    new_list.append(entry)
        if not map_entry_used:
            new_list.append(map_entry)
        self.process_maps[pid] = new_list

    def fork_pid(self, pid, ppid):
        if pid == ppid:
            return
        entry_list = self.process_maps.get(ppid, [])
        self.process_maps[pid] = copy.deepcopy(entry_list)

    def find(self, pid, addr):
        key = MapEntry(addr, addr, '')
        entry_list = self.process_maps.get(pid, [])
        pos = bisect.bisect_right(entry_list, key)
        if pos > 0 and entry_list[pos - 1].end > addr:
            return entry_list[pos - 1]
        return None

    def show(self):
        for pid in sorted(self.process_maps):
            print('  pid %d' % pid)
            for entry in self.process_maps[pid]:
                print('    map [%x-%x] %s' %
                      (entry.start, entry.end, entry.filename))


class UnwindingTimes(object):

    def __init__(self):
        self.total_time = 0
        self.count = 0
        self.max_time = 0

    def add_time(self, used_time):
        self.total_time += used_time
        self.count += 1
        self.max_time = max(self.max_time, used_time)


class CallChainNode(object):

    """ Representing a node in a call chain."""

    def __init__(self, ip, sp, filename, vaddr_in_file, function_name, map_start_addr,
                 map_end_addr):
        self.ip = ip
        self.sp = sp
        self.filename = filename
        self.vaddr_in_file = vaddr_in_file
        self.function_name = function_name
        self.map_start_addr = map_start_addr
        self.map_end_addr = map_end_addr


class SampleResult(object):

    """ Unwinding result per sample. """

    def __init__(self, pid, tid, unwinding_result, callchain):
        self.pid = pid
        self.tid = tid
        self.unwinding_result = unwinding_result
        self.callchain = callchain

    def show(self):
        print('  pid: %d' % self.pid)
        print('  tid: %d' % self.tid)
        for key, value in self.unwinding_result.items():
            print('  %s: %s' % (key, value))
        for i, node in enumerate(self.callchain):
            print('  node %d: ip 0x%x, sp 0x%x, %s (%s[+%x]), map [%x-%x]' % (
                i, node.ip, node.sp, node.function_name, node.filename, node.vaddr_in_file,
                node.map_start_addr, node.map_end_addr))


class FunctionResult(object):

    """ Unwinding result per function. """

    def __init__(self):
        # Map from Unwinding result reason to [SampleResult].
        self.sample_results = {}

    def add_sample_result(self, sample_result):
        stop_reason = sample_result.unwinding_result['stop_reason']
        result_list = self.sample_results.get(stop_reason)
        if not result_list:
            result_list = self.sample_results[stop_reason] = []
        for result in result_list:
            if result.callchain[-1].vaddr_in_file == sample_result.callchain[-1].vaddr_in_file:
                # This sample_result duplicates with an existing one.
                return
        # We don't want to store too many sample results for a function.
        if len(result_list) < 10:
            result_list.append(sample_result)

    def show(self):
        for stop_reason in sorted(self.sample_results):
            for sample_result in self.sample_results[stop_reason]:
                sample_result.show()


class FileResult(object):

    """ Unwinding result per shared library. """

    def __init__(self):
        self.function_results = {}  # Map from function_name to FunctionResult.

    def add_sample_result(self, sample_result):
        function_name = sample_result.callchain[-1].function_name
        function_result = self.function_results.get(function_name)
        if not function_result:
            function_result = self.function_results[
                function_name] = FunctionResult()
        function_result.add_sample_result(sample_result)

    def show(self):
        for function_name in sorted(self.function_results):
            print('  function %s' % function_name)
            self.function_results[function_name].show()
            print('\n')


class UnwindingResultErrorReport(object):

    """ Report time used for unwinding and unwinding result errors. """

    def __init__(self, omit_callchains_fixed_by_joiner):
        self.omit_callchains_fixed_by_joiner = omit_callchains_fixed_by_joiner
        self.process_maps = ProcessMaps()
        self.unwinding_times = UnwindingTimes()
        self.file_results = {}  # map from filename to FileResult.

    def add_sample_result(self, sample_result, joined_record):
        self.unwinding_times.add_time(int(sample_result.unwinding_result['used_time']))
        if self.should_omit(sample_result, joined_record):
            return
        filename = sample_result.callchain[-1].filename
        file_result = self.file_results.get(filename)
        if not file_result:
            file_result = self.file_results[filename] = FileResult()
        file_result.add_sample_result(sample_result)

    def should_omit(self, sample_result, joined_record):
        # 1. Can't unwind code generated in memory.
        for name in ['/dev/ashmem/dalvik-jit-code-cache', '//anon']:
            if name in sample_result.callchain[-1].filename:
                return True
        # 2. Don't report complete callchains, which can reach __libc_init or __start_thread in
        # libc.so.
        def is_callchain_complete(callchain):
            for node in callchain:
                if (node.filename.endswith('libc.so') and
                        node.function_name in ['__libc_init', '__start_thread']):
                    return True
            return False
        if is_callchain_complete(sample_result.callchain):
            return True
        # 3. Omit callchains made complete by callchain joiner.
        if self.omit_callchains_fixed_by_joiner and is_callchain_complete(joined_record.callchain):
            return True
        return False

    def show(self):
        print('Unwinding time info:')
        print('  total time: %f ms' % (self.unwinding_times.total_time / 1e6))
        print('  unwinding count: %d' % self.unwinding_times.count)
        if self.unwinding_times.count > 0:
            print('  average time: %f us' % (
                self.unwinding_times.total_time / 1e3 / self.unwinding_times.count))
        print('  max time: %f us' % (self.unwinding_times.max_time / 1e3))
        print('Process maps:')
        self.process_maps.show()
        for filename in sorted(self.file_results):
            print('filename %s' % filename)
            self.file_results[filename].show()
            print('\n')


class CallChainRecord(object):
    """ Store data of a callchain record. """

    def __init__(self):
        self.pid = None
        self.tid = None
        self.callchain = []


def parse_callchain_record(lines, i, chain_type, process_maps):
    if i == len(lines) or not lines[i].startswith('record callchain:'):
        log_fatal('unexpected dump output near line %d' % i)
    i += 1
    record = CallChainRecord()
    ips = []
    sps = []
    function_names = []
    filenames = []
    vaddr_in_files = []
    map_start_addrs = []
    map_end_addrs = []
    in_callchain = False
    while i < len(lines) and not lines[i].startswith('record'):
        line = lines[i].strip()
        items = line.split()
        if not items:
            i += 1
            continue
        if items[0] == 'pid' and len(items) == 2:
            record.pid = int(items[1])
        elif items[0] == 'tid' and len(items) == 2:
            record.tid = int(items[1])
        elif items[0] == 'chain_type' and len(items) == 2:
            if items[1] != chain_type:
                log_fatal('unexpected dump output near line %d' % i)
        elif items[0] == 'ip':
            m = re.search(r'ip\s+0x(\w+),\s+sp\s+0x(\w+)$', line)
            if m:
                ips.append(int(m.group(1), 16))
                sps.append(int(m.group(2), 16))
        elif items[0] == 'callchain:':
            in_callchain = True
        elif in_callchain:
            # "dalvik-jit-code-cache (deleted)[+346c] (/dev/ashmem/dalvik-jit-code-cache (deleted)[+346c])"
            if re.search(r'\)\[\+\w+\]\)$', line):
                break_pos = line.rfind('(', 0, line.rfind('('))
            else:
                break_pos = line.rfind('(')
            if break_pos > 0:
                m = re.match('(.+)\[\+(\w+)\]\)', line[break_pos + 1:])
                if m:
                    function_names.append(line[:break_pos].strip())
                    filenames.append(m.group(1))
                    vaddr_in_files.append(int(m.group(2), 16))
        i += 1

    for ip in ips:
        map_entry = process_maps.find(record.pid, ip)
        if map_entry:
            map_start_addrs.append(map_entry.start)
            map_end_addrs.append(map_entry.end)
        else:
            map_start_addrs.append(0)
            map_end_addrs.append(0)
    n = len(ips)
    if (None in [record.pid, record.tid] or n == 0 or len(sps) != n or
            len(function_names) != n or len(filenames) != n or len(vaddr_in_files) != n or
            len(map_start_addrs) != n or len(map_end_addrs) != n):
        log_fatal('unexpected dump output near line %d' % i)
    for j in range(n):
        record.callchain.append(CallChainNode(ips[j], sps[j], filenames[j], vaddr_in_files[j],
                                              function_names[j], map_start_addrs[j],
                                              map_end_addrs[j]))
    return i, record


def build_unwinding_result_report(record_file, omit_callchains_fixed_by_joiner):
    simpleperf_path = get_host_binary_path('simpleperf')
    args = [simpleperf_path, 'dump', record_file]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE)
    (stdoutdata, _) = proc.communicate()
    if 'record callchain' not in stdoutdata or 'record unwinding_result' not in stdoutdata:
        log_exit("Can't parse unwinding result. Because %s is not recorded using '--log debug'."
                 % record_file)
    unwinding_report = UnwindingResultErrorReport(omit_callchains_fixed_by_joiner)
    process_maps = unwinding_report.process_maps
    lines = stdoutdata.split('\n')
    i = 0
    while i < len(lines):
        if lines[i].startswith('record mmap:'):
            i += 1
            pid = None
            start = None
            end = None
            filename = None
            while i < len(lines) and not lines[i].startswith('record'):
                if lines[i].startswith('  pid'):
                    m = re.search(r'pid\s+(\d+).+addr\s+0x(\w+).+len\s+0x(\w+)', lines[i])
                    if m:
                        pid = int(m.group(1))
                        start = int(m.group(2), 16)
                        end = start + int(m.group(3), 16)
                elif lines[i].startswith('  pgoff'):
                    pos = lines[i].find('filename') + len('filename')
                    filename = lines[i][pos:].strip()
                i += 1
            if None in [pid, start, end, filename]:
                log_fatal('unexpected dump output near line %d' % i)
            process_maps.add(pid, MapEntry(start, end, filename))
        elif lines[i].startswith('record unwinding_result:'):
            i += 1
            unwinding_result = collections.OrderedDict()
            while i < len(lines) and not lines[i].startswith('record'):
                strs = (lines[i].strip()).split()
                if len(strs) == 2:
                    unwinding_result[strs[0]] = strs[1]
                i += 1
            for key in ['time', 'used_time', 'stop_reason']:
                if key not in unwinding_result:
                    log_fatal('unexpected dump output near line %d' % i)

            i, original_record = parse_callchain_record(lines, i, 'ORIGINAL_OFFLINE', process_maps)
            i, joined_record = parse_callchain_record(lines, i, 'JOINED_OFFLINE', process_maps)
            sample_result = SampleResult(original_record.pid, original_record.tid,
                                         unwinding_result, original_record.callchain)
            unwinding_report.add_sample_result(sample_result, joined_record)
        elif lines[i].startswith('record fork:'):
            i += 1
            pid = None
            ppid = None
            while i < len(lines) and not lines[i].startswith('record'):
                if lines[i].startswith('  pid'):
                    m = re.search(r'pid\s+(\w+),\s+ppid\s+(\w+)', lines[i])
                    if m:
                        pid = int(m.group(1))
                        ppid = int(m.group(2))
                i += 1
            if None in [pid, ppid]:
                log_fatal('unexpected dump output near line %d' % i)
            process_maps.fork_pid(pid, ppid)
        else:
            i += 1
    return unwinding_report


def main():
    parser = argparse.ArgumentParser(
        description='report unwinding result in profiling data')
    parser.add_argument('-i', '--record_file', nargs=1, default=['perf.data'], help="""
                        Set profiling data to report. Default is perf.data.""")
    parser.add_argument('--omit-callchains-fixed-by-joiner', action='store_true', help="""
                        Don't show incomplete callchains fixed by callchain joiner.""")
    args = parser.parse_args()
    report = build_unwinding_result_report(args.record_file[0],
                                           args.omit_callchains_fixed_by_joiner)
    report.show()

if __name__ == '__main__':
    main()
