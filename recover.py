#!/usr/bin/env python3
"""
Cache recovery tool.

Attempts to rebuild genrecache db from genrescrape program output.
"""
import argparse
import logging
import re
import shelve

from genrescrape import LOG_FMT, Scraper


GENRE_REGEX = re.compile(r'\* (.*) \(\[#(\d*)\]\((.*)\)\)')


log = logging.getLogger(__name__)


def recover_cache(from_file):
    """Search from_file for genre entries, place back into genre cache."""
    log.info('Recovering cache from %s', from_file)
    count = 0
    with shelve.open(Scraper.genre_cache_fn) as cache:
        for line in from_file:
            match = GENRE_REGEX.match(line)
            if match:
                log.debug('Found entry %r', match.groups())
                title = match.group(1)
                number = match.group(2)
                url = match.group(3)
                cache[number] = (title, url)
                count += 1

    return count


def main():
    """Entrypoint to recovercache script."""
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('filename')
    arg_parser.add_argument('-v', action='count', default=0)
    ns = arg_parser.parse_args()

    log_level = logging.WARNING - 10 * ns.v

    logging.basicConfig(level=log_level, **LOG_FMT)

    with open(ns.filename) as infile:
        recoveries = recover_cache(infile)

    print('Recovered {} entries'.format(recoveries))


if __name__ == '__main__':
    main()
