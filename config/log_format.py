"""The one console log format, shared by `main` and `Config`'s bootstrap handler.

`Config` loads before `main()` configures logging, so it attaches its own handler to emit
early warnings — and that handler must render identically to the one that replaces it,
or the first few lines of every run look like they came from a different program. That
was previously achieved by declaring the same `ColoredFormatter` twice and a comment
saying one "mirrors" the other, which is a promise no one can keep across an edit.

Deliberately a leaf: it imports nothing from the project, so `config` and `main` can both
depend on it without a cycle.
"""
from logging import StreamHandler
from typing import TextIO

from colorlog import ColoredFormatter

# noinspection SpellCheckingInspection
LOG_FORMAT = '[%(asctime)s] (%(name)s) %(log_color)s[%(levelname)s] %(message)s'
DATE_FORMAT = '%Y/%m/%d %H:%M:%S'
LOG_COLORS = {
	'DEBUG': 'cyan',
	'INFO': 'green',
	'WARNING': 'yellow',
	'ERROR': 'red',
	'CRITICAL': 'bold_red',
}


def make_handler(stream: TextIO) -> StreamHandler:
	"""A StreamHandler on `stream`, formatted the one way this project formats logs."""
	handler = StreamHandler(stream)
	handler.setFormatter(ColoredFormatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT, log_colors=LOG_COLORS))
	return handler
