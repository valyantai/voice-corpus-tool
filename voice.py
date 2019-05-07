#!/usr/bin/env python3
from __future__ import absolute_import, division, print_function

import os
import csv
import sys
import glob
import math
import tqdm
import shutil
import tempfile
import subprocess
import tables
import codecs
import numpy as np
import scipy.io.wavfile as wav

from python_speech_features import mfcc
from threading import Lock
from random import shuffle, randrange, uniform
from random import choices
from shutil import copyfile
from pydub import AudioSegment
from intervaltree import IntervalTree
from multiprocessing import cpu_count

from multiprocessing import Pool as ProcessPool
from multiprocessing.dummy import Pool as ThreadPool

def log(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

class Error(Exception):
    def __init__(self, message):
        self.message = message

class _CommandLineParserCommand(object):
    def __init__(self, name, action, description):
        self.name = name
        self.action = action
        self.description = description
        self.arguments = []
        self.options = {}
    def add_argument(self, name, type, description):
        assert type != 'bool'
        self.arguments.append(_CommandLineParserParameter(name, type, description))
    def add_option(self, name, type, description):
        self.options[name] = _CommandLineParserParameter(name, type, description)

class _CommandLineParserParameter(object):
    def __init__(self, name, type, description):
        self.name = name
        self.type = type
        self.description = description

class _CommandLineParserState(object):
    def __init__(self, tokens):
        self.tokens = tokens
        self.index = -1
    @property
    def token(self):
        return self.tokens[self.index]
    def next(self):
        self.index += 1
        return self.index < len(self.tokens)
    def prev(self):
        self.index -= 1
        return self.index >= 0


class CommandLineParser(object):
    def __init__(self):
        self.commands = {}
        self.command_list = []
        self.add_command('help', self._cmd_help, 'Display help message')

    def add_command(self, name, action, description):
        cmd = _CommandLineParserCommand(name, action, description)
        self.commands[name] = cmd
        self.command_list.append(cmd)
        return cmd

    def add_group(self, caption):
        self.command_list.append(caption)

    def _parse_value(self, state, value_type):
        if value_type == 'bool':
            return True
        if not state.next():
            return None
        try:
            if value_type == 'int':
                return int(state.token)
            if value_type == 'float':
                return float(state.token)
        except:
            state.prev()
            return None
        return state.token

    def _parse(self, state):
        while state.next():
            if not state.token in self.commands:
                return "Unrecognized command: %s" % state.token
            cmd = self.commands[state.token]
            arg_values = []
            for arg in cmd.arguments:
                arg_value = self._parse_value(state, arg.type)
                if not arg_value:
                    return "Problem parsing argument %s of command %s" % (arg.name, cmd.name)
                arg_values.append(arg_value)
            options = {}
            while state.next() and state.token[0] == '-':
                opt_name = state.token[1:]
                if not opt_name in cmd.options:
                    return "Unknown option -%s for command %s" % (opt_name, cmd.name)
                opt = cmd.options[opt_name]
                opt_value = self._parse_value(state, opt.type)
                if opt_value == None:
                    return "Unable to parse %s value for option -%s of command %s" % (opt.type, opt.name, cmd.name)
                options[opt_name] = opt_value
            state.prev()
            result = cmd.action(*arg_values, **options)
            if result:
                return result
        return None

    def parse(self, tokens):
        state = _CommandLineParserState(tokens)
        result = self._parse(state)
        if result:
            log(result)
            log()
            self._cmd_help()
            return

    def _cmd_help(self):
        log('A tool to apply a series of commands to a collection of samples.')
        log('Usage: voice.py (command <arg1> <arg2> ... [-opt1 [<value>]] [-opt2 [<value>]] ...)*\n')
        log('Commands:')
        for cmd in self.command_list:
            log()
            if isinstance(cmd, str):
                log(cmd + ':')
                continue
            arg_desc = ' '.join('<%s>' % arg.name for arg in cmd.arguments)
            opt_desc = ' '.join(('[-%s%s]' % (opt.name, ' <%s>' % opt.name if opt.type != 'bool' else '')) for _, opt in cmd.options.items())
            log('  %s %s %s' % (cmd.name, arg_desc, opt_desc))
            log('\t%s' % cmd.description)
            if len(cmd.arguments) > 0:
                log('\tArguments:')
                for arg in cmd.arguments:
                    log('\t\t%s: %s - %s' % (arg.name, arg.type, arg.description))
            if len(cmd.options) > 0:
                log('\tOptions:')
                for _, opt in cmd.options.items():
                    log('\t\t-%s: %s - %s' % (opt.name, opt.type, opt.description))

tmp_dir = None
tmp_index = 0
tmp_lock = Lock()

def get_tmp_filename():
    global tmp_index, tmp_dir
    with tmp_lock:
        if not tmp_dir:
            tmp_dir = tempfile.mkdtemp(dir=os.getcwd(), prefix='.__tmp')
        tmp_index += 1
        return os.path.join(tmp_dir, '%d.wav' % tmp_index)

def to_float(str, ifnot):
    try:
        f = float(value)
        return f
    except:
        return ifnot

def to_int(str, ifnot):
    try:
        n = int(value)
        return n
    except:
        return ifnot

class WavFile(object):
    def __init__(self, filename=None, filesize=-1, duration=-1, enforce_tmp=False):
        self.file_is_tmp = not filename or enforce_tmp
        self.filename = os.path.abspath(filename) if filename else get_tmp_filename()
        self._filesize = filesize
        self._duration = duration
        self._stats = None

    def __del__(self):
        if self.file_is_tmp and os.path.exists(self.filename):
            os.remove(self.filename)

    def save_as(self, filename):
        filename = os.path.abspath(filename)
        if self.file_is_tmp:
            os.rename(self.filename, filename)
            self.filename = filename
            self.file_is_tmp = False
            return self
        file = WavFile(filename=filename)
        copyfile(self.filename, file.filename)
        file._stats = self._stats
        file._duration = self._duration
        file._filesize = self._filesize
        return file

    @property
    def stats(self):
        if not self._stats:
            entries = subprocess.check_output(['soxi', self.filename], stderr=subprocess.STDOUT).decode()
            entries = entries.strip().split('\n')
            entries = [e.split(':')[:2] for e in entries]
            entries = [(e[0].strip(), e[1].strip()) for e in entries if len(e) == 2]
            self._stats = { key: value for (key, value) in entries }
        return self._stats

    @property
    def duration(self):
        if self._duration < 0:
            self._duration = float(subprocess.check_output(['soxi', '-D', self.filename]).strip())
        return self._duration

    @property
    def filesize(self):
        if self._filesize < 0:
            self._filesize = os.path.getsize(self.filename)
        return self._filesize

    @property
    def volume(self):
        return float(self.stats['Volume adjustment'])

class Sample(object):
    def __init__(self, file, transcript=None, tags=[]):
        self.file = file
        self.transcript = transcript
        self.tags = tags
        self.original_name = self.file.filename
        self.effects = ''

    def write(self, filename=None):
        if len(self.effects) > 0:
            file = WavFile(filename=filename)
            subprocess.check_output(['sox', self.file.filename, file.filename] + self.effects.strip().split(' '))
            self.effects = ''
            self.file = file
        elif filename:
            self.file = self.file.save_as(filename)

    def pipe(self, commands):
        self.write()
        file = WavFile()
        def subst(item):
            if item == 'IN':
                return self.file.filename
            elif item == 'OUT':
                return file.filename
            else:
                return item
        first = None
        last = None
        for i, command in enumerate(commands):
            command = [subst(item) for item in command]
            last = subprocess.Popen(command, stdin=last.stdout if last else None, stdout=subprocess.PIPE if first is None else None)
            if first is None:
                first = last
        first.wait()
        self.file = file

    def add_sox_effect(self, effect):
        self.effects += ' %s' % effect

    def read_audio_segment(self):
        self.write()
        return AudioSegment.from_file(self.file.filename, format="wav")

    def write_audio_segment(self, segment):
        self.file = WavFile()
        segment.export(self.file.filename, format="wav")

    def clone(self):
        sample = Sample(self.file, transcript=self.transcript, tags=self.tags)
        sample.original_name = self.original_name
        sample.effects = self.effects
        return sample

    def __str__(self):
        return 'Filename: "%s"\nTranscript: "%s"' % (self.file.filename, self.transcript)

class DataSetBuilder(CommandLineParser):
    def __init__(self):
        super(DataSetBuilder, self).__init__()
        cmd = self.add_command('add', self._add, 'Adds samples to current buffer')
        cmd.add_argument('source', 'string', 'Name of a named buffer or filename of a CSV file or WAV file (wildcards supported)')

        self.add_group('Buffer operations')

        cmd = self.add_command('shuffle', self._shuffle, 'Randoimize order of the sample buffer')

        cmd = self.add_command('order', self._order, 'Order samples in buffer by length')

        cmd = self.add_command('reverse',self._reverse, 'Reverse order of samples in buffer')

        cmd = self.add_command('take', self._take, 'Take given number of samples from the beginning of the buffer as new buffer')
        cmd.add_argument('number', 'int', 'Number of samples')

        cmd = self.add_command('repeat', self._repeat, 'Repeat samples of current buffer <number> times as new buffer')
        cmd.add_argument('number', 'int', 'How often samples of the buffer should get repeated')

        cmd = self.add_command('skip', self._skip, 'Skip given number of samples from the beginning of current buffer')
        cmd.add_argument('number', 'int', 'Number of samples')

        cmd = self.add_command('find', self._find, 'Drop all samples, whose transcription does not contain a keyword' )
        cmd.add_argument('keyword', 'string', 'Keyword to look for in transcriptions')

        cmd = self.add_command('tagged', self._tagged, 'Keep only samples with a specific tag' )
        cmd.add_argument('tag', 'string', 'Tag to look for')

        cmd = self.add_command('settag', self._set_tag, 'Sets a tag on all samples of current buffer' )
        cmd.add_argument('tag', 'string', 'Tag to set')

        cmd = self.add_command('clear', self._clear, 'Clears sample buffer')

        self.add_group('Named buffers')

        cmd = self.add_command('set', self._set, 'Replaces named buffer with portion of buffer')
        cmd.add_argument('name', 'string', 'Name of the named buffer')
        cmd.add_option('percent', 'int', 'Percentage of samples from the beginning of buffer. If omitted, complete buffer.')

        cmd = self.add_command('stash', self._stash, 'Moves buffer portion to named buffer. Moved samples will not remain in main buffer.')
        cmd.add_argument('name', 'string', 'Name of the named buffer')
        cmd.add_option('percent', 'int', 'Percentage of samples from the beginning of buffer. If omitted, complete buffer.')

        cmd = self.add_command('push', self._push, 'Appends portion of buffer samples to named buffer')
        cmd.add_argument('name', 'string', 'Name of the named buffer')
        cmd.add_option('percent', 'int', 'Percentage of samples from the beginning of buffer. If omitted, complete buffer.')

        cmd = self.add_command('slice', self._slice, 'Moves portion of named buffer to current buffer')
        cmd.add_argument('name', 'string', 'Name of the named buffer')
        cmd.add_argument('percent', 'int', 'Percentage of samples from the beginning of named buffer')

        cmd = self.add_command('drop', self._drop, 'Drops named buffer')
        cmd.add_argument('name', 'string', 'Name of the named buffer')

        self.add_group('Output')

        cmd = self.add_command('print', self._print, 'Prints list of samples in current buffer')

        cmd = self.add_command('play', self._play, 'Play samples of current buffer')

        cmd = self.add_command('pipe', self._pipe, 'Pipe raw sample data of current buffer to stdout. Could be piped to "aplay -r 44100 -c 2 -t raw -f s16".')

        cmd = self.add_command('write', self._write, 'Write samples of current buffer to disk')
        cmd.add_argument('dir_name', 'string', 'Path to the new sample directory. The directory and a file with the same name plus extension ".csv" should not exist.')

        cmd = self.add_command('hdf5', self._hdf5, 'Write samples to hdf5 MFCC feature DB that can be used by DeepSpeech')
        cmd.add_argument('alphabet_path', 'string', 'Path to DeepSpeech alphabet file to use for transcript mapping')
        cmd.add_argument('hdf5_path', 'string', 'Target path of hdf5 feature DB')
        cmd.add_option('ninput', 'int', 'Number of MFCC features (defaults to 26)')
        cmd.add_option('ncontext', 'int', 'Number of frames in context window (defaults to 9)')

        self.add_group('Effects')

        cmd = self.add_command('reverb', self._reverb, 'Adds reverberation to buffer samples')
        cmd.add_option('wet_only', 'bool', 'If to strip source signal on output')
        cmd.add_option('reverberance', 'float', 'Reverberance factor (between 0.0 to 1.0)')
        cmd.add_option('hf_damping', 'float', 'HF damping factor (between 0.0 to 1.0)')
        cmd.add_option('room_scale', 'float', 'Room scale factor (between 0.0 to 1.0)')
        cmd.add_option('stereo_depth', 'float', 'Stereo depth factor (between 0.0 to 1.0)')
        cmd.add_option('pre_delay', 'int', 'Pre delay in ms')
        cmd.add_option('wet_gain', 'float', 'Wet gain in dB')

        cmd = self.add_command('echo', self._echo, 'Adds an echo effect to buffer samples')
        cmd.add_argument('gain_in', 'float', 'Gain in')
        cmd.add_argument('gain_out', 'float', 'Gain out')
        cmd.add_argument('delay_decay', 'string', 'Comma separated delay decay pairs - at least one (e.g. 10,0.1,20,0.2)')

        cmd = self.add_command('speed', self._speed, 'Adds an speed effect to buffer samples')
        cmd.add_argument('factor', 'float', 'Speed factor to apply')

        cmd = self.add_command('pitch', self._pitch, 'Adds a pitch effect to buffer samples')
        cmd.add_argument('cents', 'int', 'Cents (100th of a semi-tome) of shift to apply')

        cmd = self.add_command('tempo', self._tempo, 'Adds a tempo effect to buffer samples')
        cmd.add_argument('factor', 'float', 'Tempo factor to apply')

        cmd = self.add_command('distcompression', self._dist_compression, 'Distortion by mp3 compression')
        cmd.add_argument('kbit', 'int', 'Virtual bandwidth in kBit/s')

        cmd = self.add_command('distrate', self._dist_rate, 'Distortion by resampling')
        cmd.add_argument('rate', 'int', 'Sample rate to apply')

        cmd = self.add_command('sox', self._sox, 'Adds a SoX effect to buffer samples')
        cmd.add_argument('effect', 'string', 'SoX effect name')
        cmd.add_argument('args', 'string', 'Comma separated list of SoX effect parameters (no white space allowed)')

        cmd = self.add_command('augment', self._augment, 'Augment samples of current buffer with noise')
        cmd.add_argument('source', 'string', 'CSV file with samples to augment onto current sample buffer')
        cmd.add_option('times', 'int', 'How often to apply the augmentation source to the sample buffer')
        cmd.add_option('gain', 'float', 'How much gain (in dB) to apply to augmentation audio before overlaying onto buffer samples')

        cmd = self.add_command('augment_combination', self._augment_combination, 'Augment samples of current buffer with random combination of noise samples')
        cmd.add_argument('source', 'string', 'CSV file with samples to augment onto current sample buffer')
        cmd.add_option('combination_count', 'int', 'How many noise samples are mixed before overlayed onto buffer samples')
        cmd.add_option('gain', 'float',
                       'How much gain (in dB) to apply to augmentation audio before overlaying onto buffer samples')

        self.named_buffers = {}
        self.samples = []

    def _map(self, message, lst, fun, use_processes=False, map_fun=lambda x: x, worker_count=0, total=-1):
        worker_count = cpu_count() if worker_count < 1 else worker_count
        pool = ProcessPool(worker_count) if use_processes else ThreadPool(worker_count)
        results = []
        for result in self._progress(message, pool.imap_unordered(fun, lst), total=len(lst) if total < 0 else total):
            results.append(map_fun(result))
        pool.close()
        pool.join()
        return results

    def _progress(self, message, lst, total=-1):
        log(message)
        return tqdm.tqdm(lst, ascii=True, ncols=100, mininterval=60.0, total=len(lst) if total < 0 else total)

    def _clone_buffer(self, buffer):
        samples = []
        for sample in buffer:
            samples.append(sample.clone())
        return samples

    def _load_samples(self, source):
        ext = source[-4:].lower()
        if ext == '.csv':
            parent = os.path.dirname(os.path.normpath(source))
            checkrelative = lambda filename: filename if os.path.isabs(filename) else os.path.normpath(os.path.join(parent, filename))
            with open(source) as source_f:
                reader = csv.reader(source_f, delimiter=',')
                rows = list(reader)
                head = rows[0]
                rows = rows[1:]
                filename_index   = head.index('wav_filename')
                filesize_index   = head.index('wav_filesize') if 'wav_filesize' in head else None
                duration_index   = head.index('duration')     if 'duration'     in head else None
                transcript_index = head.index('transcript')   if 'transcript'   in head else None
                tags_index       = head.index('tags')         if 'tags'         in head else None
                samples = [Sample(WavFile(filename=checkrelative(row[filename_index]),
                                          filesize=to_int(row[filesize_index], -1) if filesize_index else -1,
                                          duration=to_float(row[duration_index], -1) if duration_index else -1),
                                  transcript=row[transcript_index] if transcript_index else None,
                                  tags=row[tags_index].split() if tags_index else []) for row in rows]
        elif source in self.named_buffers:
            samples = self._clone_buffer(self.named_buffers[source])
        else:
            samples = glob.glob(source)
            samples = [Sample(WavFile(filename=s), '') for s in samples]
        if len(samples) == 0:
            raise Error('No samples found!')
        return samples

    def _add(self, source):
        samples = self._load_samples(source)
        self.samples.extend(samples)
        log('Added %d samples to buffer.' % len(samples))

    def _shuffle(self):
        shuffle(self.samples)
        log('Shuffled buffer.')

    def _order(self):
        self.samples = sorted(self.samples, key=lambda s: s.file.filesize)
        log('Ordered buffer by file lenghts.')

    def _reverse(self):
        self.samples.reverse()
        log('Reversed buffer.')

    def _take(self, number):
        self.samples = self.samples[:number]
        log('Took %d samples as new buffer.' % number)

    def _repeat(self, number):
        samples = self.samples[:]
        for _ in range(number - 1):
            for sample in self.samples:
                samples.append(sample.clone())
        self.samples = samples
        log('Repeated samples in buffer %d times as new buffer.' % number)

    def _skip(self, number):
        self.samples = self.samples[number:]
        log('Removed first %d samples from buffer.' % number)

    def _clear(self):
        self.samples = []
        log('Removed all samples from buffer.')

    def _set(self, name, percent=100):
        upto = int(math.ceil(percent * len(self.samples) / 100.0))
        self.named_buffers[name] = self._clone_buffer(self.samples[:upto])
        log('Copied first %d samples of current buffer to named buffer "%s" (replacing its contents).' % (upto, name))

    def _stash(self, name, percent=100):
        upto = int(math.ceil(percent * len(self.samples) / 100.0))
        self.named_buffers[name] = self.samples[:upto]
        self.samples = self.samples[upto:]
        log('Moved first %d samples of current buffer to named buffer "%s" (replacing its contents).' % (upto, name))

    def _push(self, name, percent=100):
        upto = int(math.ceil(percent * len(self.samples) / 100.0))
        if not name in self.named_buffers:
            self.named_buffers[name] = []
        self.named_buffers[name].extend(self._clone_buffer(self.samples[:upto]))
        log('Appended copies of first %d samples of current buffer to named buffer "%s".' % (upto, name))

    def _slice(self, name, percent):
        buffer = self.named_buffers[name]
        if buffer:
            upto = int(math.ceil(percent * len(buffer) / 100.0))
            self.named_buffers[name] = buffer[upto:]
            self.samples.extend(buffer[:upto])
            log('Moved first %d samples of named buffer "%s" to end of current buffer.' % (upto, name))
        else:
            log('No buffer of name "%s"' % name)

    def _drop(self, name):
        del self.named_buffers[name]
        log('Dropped named buffer "%s".' % name)

    def _find(self, keyword):
        self.samples = [s for s in self.samples if keyword in s.transcript]
        log('Found %d samples containing keyword "%s".' % (len(self.samples), keyword))

    def _tagged(self, tag):
        self.samples = [s for s in self.samples if tag in s.tags]
        log('Found %d samples with tag "%s".' % (len(self.samples), tag))

    def _set_tag(self, tag):
        c = 0
        for s in self.samples:
            if not tag in s.tags:
                c = c + 1
                s.tags.append(tag)
        log('Tagged %d samples as "%s".' % (c, tag))

    def _print(self):
        for s in self.samples:
            log(s)
        log('Printed %d samples.' % len(self.samples))

    def _play(self):
        log('Playing:')
        for s in self.samples:
            s.write()
            log(s)
            subprocess.call(['play', '-q', s.file.filename])
        log('Played %d samples.' % len(self.samples))

    def _pipe(self):
        log('Piping:')
        for s in self.samples:
            s.write()
            log(s)
            seg = s.read_audio_segment()
            sys.stdout.write(seg.set_sample_width(2).set_frame_rate(88200).raw_data)
        log('Piped %d samples.' % len(self.samples))

    def _write(self, dir_name):
        parent, name = os.path.split(os.path.normpath(dir_name))
        csv_filename = os.path.join(parent, name + '.csv')
        if os.path.exists(dir_name) or os.path.exists(csv_filename):
            return 'Cannot write buffer, as either "%s" or "%s" already exist.' % (dir_name, csv_filename)
        os.makedirs(dir_name)
        samples = [(i, sample) for i, sample in enumerate(self.samples)]
        with open(csv_filename, 'w') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['wav_filename', 'wav_filesize', 'transcript', 'tags', 'duration'])
            def write_sample(i_sample):
                i, sample = i_sample
                sample_name = os.path.basename(sample.original_name) #'sample-%d.wav' % i
                sample.write(filename=os.path.join(dir_name, sample_name))
                writer.writerow([os.path.join(name, sample_name),
                                 sample.file.filesize,
                                 sample.transcript,
                                 ' '.join(sample.tags),
                                 sample.file.duration])
            self._map('Writing samples...', samples, write_sample)
        log('Wrote %d samples to directory "%s" and listed them in CSV file "%s".' % (len(self.samples), dir_name, csv_filename))

    def _hdf5(self, alphabet_path, hdf5_path, ninput=26, ncontext=9):
        skipped = []
        str_to_label = {}
        alphabet_size = 0
        with codecs.open(alphabet_path, 'r', 'utf-8') as fin:
            for line in fin:
                if line[0:2] == '\\#':
                    line = '#\n'
                elif line[0] == '#':
                    continue
                str_to_label[line[:-1]] = alphabet_size
                alphabet_size += 1

        def process_sample(sample):
            sample.write()
            samplerate, audio = wav.read(sample.file.filename)
            features = mfcc(audio, samplerate=samplerate, numcep=ninput)[::2]
            empty_context = np.zeros((ncontext, ninput), dtype=features.dtype)
            features = np.concatenate((empty_context, features, empty_context))
            transcript = np.asarray([str_to_label[c] for c in sample.transcript])
            if (2*ncontext + len(features)) < len(transcript):
                skipped.append(sample.original_name)
                return None
            return features, len(features), transcript, len(transcript)

        out_data = self._map('Computing MFCC features...', self.samples, process_sample)
        out_data = [s for s in out_data if s is not None]
        if len(skipped) > 0:
            log('WARNING - Skipped %d samples that had been too short for their transcription:' % len(skipped))
            for s in skipped:
                log(' - Sample origin: "%s".' % s)
        if len(out_data) <= 0:
            log('No samples written to feature DB "%s".' % hdf5_path)
            return
        # list of tuples -> tuple of lists
        features, features_len, transcript, transcript_len = zip(*out_data)

        log('Writing feature DB...')
        with tables.open_file(hdf5_path, 'w') as file:
            features_dset = file.create_vlarray(file.root, 'features', tables.Float32Atom(), filters=tables.Filters(complevel=1))
            # VLArray atoms need to be 1D, so flatten feature array
            for f in features:
                features_dset.append(np.reshape(f, -1))
            features_len_dset = file.create_array(file.root, 'features_len', features_len)

            transcript_dset = file.create_vlarray(file.root, 'transcript', tables.Int32Atom(), filters=tables.Filters(complevel=1))
            for t in transcript:
                transcript_dset.append(t)

            transcript_len_dset = file.create_array(file.root, 'transcript_len', transcript_len)
        log('Wrote features of %d samples to feature DB "%s".' % (len(features), hdf5_path))

    def _reverb(self, wet_only=False, reverberance=0.5, hf_damping=0.5, room_scale=1.0, stereo_depth=1.0, pre_delay=0, wet_gain=0):
        effect = 'reverb %s%d %d %d %d %d %d' % \
            ('-w ' if wet_only else '', int(reverberance*100.0), int(hf_damping*100.0), int(room_scale*100.0), int(stereo_depth*100.0), pre_delay, wet_gain)
        for s in self.samples:
            s.add_sox_effect(effect)
        log('Added reverberation to %d samples in buffer.' % len(self.samples))

    def _echo(self, gain_in, gain_out, delay_decay):
        delay_decay = delay_decay.split(',')
        assert len(delay_decay) % 2 == 0
        assert len(delay_decay) > 1
        effect = 'echo %f %f %s' % (gain_in, gain_out, ' '.join(delay_decay))
        for s in self.samples:
            s.add_sox_effect(effect)
        log('Added echo effect to %d samples in buffer.' % len(self.samples))

    def _speed(self, factor):
        effect = 'speed %f' % factor
        for s in self.samples:
            s.add_sox_effect(effect)
        log('Added speed effect to %d samples in buffer.' % len(self.samples))

    def _pitch(self, cents):
        for s in self.samples:
            random_cents = randrange(-1 * cents, cents)
            print('pitch {} chosen from interval {} and {} for sample {}'.format(random_cents, -1 * cents, cents, s.original_name))
            effect = 'pitch %d' % random_cents
            s.add_sox_effect(effect)
        log('Added pitch effect to %d samples in buffer.' % len(self.samples))

    def _tempo(self, factor):
        for s in self.samples:
            random_factor = uniform(2.0 - factor, factor)
            print('tempo {} chosen from interval {} and {}'.format(random_factor, 2.0 - factor, factor))
            effect = 'tempo -s %f' % random_factor
            s.add_sox_effect(effect)
        log('Added tempo effect to %d samples in buffer.' % len(self.samples))

    def _dist_compression(self, kbit):
        self._map('Distorting samples...',
                  self.samples,
                  lambda s: s.pipe([['sox', 'IN', '-t', 'mp3', '-C', str(kbit), '-'],
                                    ['sox', '-t', 'mp3', '-', 'OUT']]))
        log('Distorted %d samples in buffer by mp3 compression.' % len(self.samples))

    def _dist_rate(self, rate):
        self._map('Distorting samples...',
                  self.samples,
                  lambda s: s.add_sox_effect('rate %d rate %d' % (rate, int(s.file.stats['Sample Rate']))))
        log('Distorted %d samples in buffer by resampling to different sample rate.' % len(self.samples))

    def _sox(self, effect, args):
        effect = '%s %s' % (effect, ' '.join(args.split(',')))
        for s in self.samples:
            s.add_sox_effect(effect)
        log('Added %s effect to %d samples in buffer.' % (effect, len(self.samples)))

    def _augment(self, source, times=1, gain=-8):
        aug_samples = self._load_samples(source)
        tree = IntervalTree()

        aug_durs = self._map('Reading augmentation sample durations...', aug_samples, lambda s: int(math.ceil(s.file.duration * 1000.0)))
        total_aug_dur = 0
        position = 0
        for i, sample in enumerate(aug_samples):
            duration = aug_durs[i]
            if duration > 0:
                total_aug_dur += duration
                tree[position:position+duration] = sample
                position += duration

        def prepare_sample(s):
            s.write()
            return int(math.ceil(s.file.duration * 1000.0))
        orig_durs = self._map('Finalizing buffer samples...', self.samples, prepare_sample)
        total_orig_dur = sum(orig_durs)

        augmentations = []
        position = 0
        for i, sample in self._progress('Computing intervals...',
                                        enumerate(self.samples),
                                        total=len(self.samples)):
            orig_dur = orig_durs[i]
            sub_pos = position
            overlays = []
            for _ in range(times):
                inters = tree[sub_pos:sub_pos + orig_dur]
                for inter in inters:
                    offset = inter.begin - sub_pos
                    overlays.append((offset, inter.data.file.filename))
                sub_pos = (sub_pos + total_orig_dur) % total_aug_dur
            position += orig_dur
            augmentations.append((i, sample.file.filename, get_tmp_filename(), overlays, gain))

        def exchange_file(index_filename):
            index, filename = index_filename
            sample = self.samples[index]
            orig_file = sample.file
            sample.file = WavFile(filename,
                                  filesize=orig_file.filesize,
                                  duration=orig_file.duration,
                                  enforce_tmp=True)

        self._map('Augmenting samples...',
                  augmentations,
                  augment_sample,
                  use_processes=True,
                  map_fun=exchange_file)
        log('Augmented %d samples in buffer.' % len(self.samples))

    def _augment_combination(self, source, combination_count=10, gain=-8):
        aug_samples = self._load_samples(source)

        augmentations = []
        for i, sample in self._progress('Computing intervals...',
                                        enumerate(self.samples),
                                        total=len(self.samples)):
            overlays = []
            chosen_aug_samples = choices(aug_samples, k=combination_count)
            for chosen_aug_sample in chosen_aug_samples:
                overlays.append(chosen_aug_sample.file.filename)

            augmentations.append((i, sample.file.filename, get_tmp_filename(), overlays, gain))

        def exchange_file(index_filename):
            index, filename = index_filename
            sample = self.samples[index]
            orig_file = sample.file
            sample.file = WavFile(filename,
                                  filesize=orig_file.filesize,
                                  duration=orig_file.duration,
                                  enforce_tmp=True)

        self._map('Augmenting samples...',
                  augmentations,
                  augment_sample_combination,
                  use_processes=True,
                  map_fun=exchange_file)
        log('Augmented %d samples in buffer.' % len(self.samples))


def augment_sample_combination(augmentation):
    index, src_file, dst_file, overlays, gain = augmentation
    orig_seg = AudioSegment.from_file(src_file, format="wav")
    aug_seg = AudioSegment.silent(duration=len(orig_seg))
    position=0
    for overlay in overlays:
        overlay_file = overlay
        overlay_seg = AudioSegment.from_file(overlay_file, format="wav")
        aug_seg = aug_seg.overlay(overlay_seg, position=position)
        if(position + len(overlay_seg)) > len(aug_seg):
            aug_seg = aug_seg.overlay(overlay_seg[len(aug_seg)-position:], position=0)
        position = (position + len(overlay_seg)) % len(aug_seg)
    aug_seg = aug_seg + (orig_seg.dBFS - aug_seg.dBFS + gain)
    orig_seg = orig_seg.overlay(aug_seg)
    orig_seg.export(dst_file, format="wav")
    return index, dst_file


def augment_sample(augmentation):
    index, src_file, dst_file, overlays, gain = augmentation
    orig_seg = AudioSegment.from_file(src_file, format="wav")
    aug_seg = AudioSegment.silent(duration=len(orig_seg))
    for overlay in overlays:
        offset, overlay_file = overlay
        overlay_seg = AudioSegment.from_file(overlay_file, format="wav")
        if offset < 0:
            overlay_seg = overlay_seg[-offset:]
            offset = 0
        aug_seg = aug_seg.overlay(overlay_seg, position=offset)
    aug_seg = aug_seg + (orig_seg.dBFS - aug_seg.dBFS + gain)
    orig_seg = orig_seg.overlay(aug_seg)
    orig_seg.export(dst_file, format="wav")
    return (index, dst_file)


def main():
    parser = DataSetBuilder()
    parser.parse(sys.argv[1:])

if __name__ == '__main__' :
    try:
        main()
    except KeyboardInterrupt:
        log('Interrupted by user')
    if tmp_dir:
        shutil.rmtree(tmp_dir)
