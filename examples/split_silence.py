#!/usr/bin/env python
from __future__ import unicode_literals

import argparse
import errno
import ffmpeg
import json
import logging
import os
import re
import subprocess
import sys


logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__file__)
logger.setLevel(logging.INFO)

DEFAULT_DURATION = 0.3
DEFAULT_THRESHOLD = -30

parser = argparse.ArgumentParser(description='Split media into separate chunks wherever silence occurs')
parser.add_argument('in_filename', help='Input filename (`-` for stdin)')
parser.add_argument('out_pattern', help='Output filename pattern (e.g. `out/chunk_{:04d}.wav`)')
parser.add_argument('--silence-threshold', default=DEFAULT_THRESHOLD, type=int, help='Silence threshold (in dB)')
parser.add_argument('--silence-duration', default=DEFAULT_DURATION, type=float, help='Silence duration')
parser.add_argument('--start-time', type=float, help='Start time (seconds)')
parser.add_argument('--end-time', type=float, help='End time (seconds)')
parser.add_argument('--padding', type=float, default=0., help='Output silence padding (seconds)')
parser.add_argument('--metadata-filename', help='Optional metadata output file')
parser.add_argument('-v', dest='verbose', action='store_true', help='Verbose mode')


silence_start_re = re.compile(' silence_start: (?P<start>[0-9]+(\.?[0-9]*))$')
silence_end_re = re.compile(' silence_end: (?P<end>[0-9]+(\.?[0-9]*)) ')
total_duration_re = re.compile(
    'size=[^ ]+ time=(?P<hours>[0-9]{2}):(?P<minutes>[0-9]{2}):(?P<seconds>[0-9\.]{5}) bitrate=')


def _logged_popen(cmd_line, *args, **kwargs):
    logger.debug('Running command: {}'.format(subprocess.list2cmdline(cmd_line)))
    return subprocess.Popen(cmd_line, *args, **kwargs)


def get_chunk_times(in_filename, silence_threshold, silence_duration, start_time=None, end_time=None):
    input_kwargs = {}
    if start_time is not None:
        input_kwargs['ss'] = start_time
    else:
        start_time = 0.
    if end_time is not None:
        input_kwargs['t'] = end_time - start_time

    child = _logged_popen(
        (ffmpeg
            .input(in_filename, **input_kwargs)
            .filter_('silencedetect', n='{}dB'.format(silence_threshold), d=silence_duration)
            .output('-', format='null')
            .compile()
        ) + ['-nostats'],  # FIXME: use .nostats() once it's implemented in ffmpeg-python.
        stderr=subprocess.PIPE
    )
    output = child.communicate()[1].decode('utf-8')
    if child.returncode != 0:
        sys.stderr.write(output)
        sys.exit(1)
    logger.debug(output)
    lines = output.splitlines()

    # Chunks start when silence ends, and chunks end when silence starts.
    chunk_starts = []
    chunk_ends = []
    for line in lines:
        silence_start_match = silence_start_re.search(line)
        silence_end_match = silence_end_re.search(line)
        total_duration_match = total_duration_re.search(line)
        if silence_start_match:
            chunk_ends.append(float(silence_start_match.group('start')))
            if len(chunk_starts) == 0:
                # Started with non-silence.
                chunk_starts.append(start_time or 0.)
        elif silence_end_match:
            chunk_starts.append(float(silence_end_match.group('end')))
        elif total_duration_match:
            hours = int(total_duration_match.group('hours'))
            minutes = int(total_duration_match.group('minutes'))
            seconds = float(total_duration_match.group('seconds'))
            end_time = hours * 3600 + minutes * 60 + seconds

    if len(chunk_starts) == 0:
        # No silence found.
        chunk_starts.append(start_time)

    if len(chunk_starts) > len(chunk_ends):
        # Finished with non-silence.
        chunk_ends.append(end_time or 10000000.)

    return list(zip(chunk_starts, chunk_ends))


def _makedirs(path):
    """Python2-compatible version of ``os.makedirs(path, exist_ok=True)``."""
    try:
        if path:
            os.makedirs(path)
    except OSError as exc:
        if exc.errno != errno.EEXIST or not os.path.isdir(path):
            raise


def split_audio(
    in_filename,
    out_pattern,
    silence_threshold=DEFAULT_THRESHOLD,
    silence_duration=DEFAULT_DURATION,
    start_time=None,
    end_time=None,
    padding=0.,
    metadata_filename=None,
    verbose=False,
):
    chunk_times = get_chunk_times(in_filename, silence_threshold, silence_duration, start_time, end_time)

    metadata = []
    for i, (start_time, end_time) in enumerate(chunk_times):
        time = end_time - start_time
        out_filename = out_pattern.format(i, i=i)
        _makedirs(os.path.dirname(out_filename))

        start_text = '{:.04f}'.format(start_time)
        end_text = '{:.04f}'.format(end_time)
        duration_text = '{:.04f}'.format(time)
        metadata.append({
            'filename': out_filename,
            'start': start_text,
            'end': end_text,
            'duration': duration_text,
        })
        logger.info('{}: start={}, end={}, duration={}'.format(out_filename, start_text, end_text, duration_text))

        input = ffmpeg.input(in_filename, ss=start_time, t=time)
        if padding > 0.:
            silence = ffmpeg.input('anullsrc', format='lavfi', t=padding)
            input = ffmpeg.concat(silence, input, silence, v=0, a=1)

        child = _logged_popen(
            (input
                .output(out_filename)
                .overwrite_output()
                .compile()
            ),
            stdout=subprocess.PIPE if not verbose else None,
            stderr=subprocess.PIPE if not verbose else None,
        )
        out = child.communicate()
        if child.returncode != 0:
            if not verbose:
                sys.stderr.write(out[1].decode('utf-8'))

    if metadata_filename is not None:
        _makedirs(os.path.dirname(metadata_filename))
        with open(metadata_filename, 'w') as f:
            json.dump(metadata, f)


if __name__ == '__main__':
    kwargs = vars(parser.parse_args())
    if kwargs['verbose']:
        logging.basicConfig(level=logging.DEBUG, format='%(levels): %(message)s')
        logger.setLevel(logging.DEBUG)
    split_audio(**kwargs)
