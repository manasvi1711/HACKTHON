"""Picklable stand-in for a parser that hangs (runs in the parser worker process)."""
import time


def hanging_parse(*args, **kwargs):
    time.sleep(30)
