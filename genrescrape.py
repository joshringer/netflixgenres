#!/usr/bin/env python3
"""
Netflix Genre Scraper.

This tool scrapes the Netflix website to gather a list of available genres
and links to their respective pages. Please use sparingly to avoid annoying
the Netflix webservers.
"""
import argparse
import logging
import os.path
import shelve
import sys
from datetime import datetime, timezone
from getpass import getpass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from requests import Session


LOG_FMT = {
    'style': '{',
    'format': '{levelname:1.1}: {message}',
}


log = logging.getLogger(__name__)


class FormParser(HTMLParser):
    """Basic serialization of HTML forms."""

    def reset(self):
        self.form_data = {}
        self._current_form = None
        super().reset()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self._current_form = attrs.get('id', len(self.form_data))
            self.form_data[self._current_form] = {'attrs': attrs, 'fields': {}}
            log.debug('Form %s open', self._current_form)

        if self._current_form is not None and 'name' in attrs:
            log.debug('Form  %s: %r', tag, attrs)
            self.form_data[self._current_form]['fields'][attrs['name']] = attrs.get('value')

    def handle_endtag(self, tag):
        if tag == 'form':
            log.debug('Form %s close', self._current_form)
            self._current_form = None


class ProfileListParser(HTMLParser):
    """Parse "Who's Watching" profile list."""

    def reset(self):
        self.profiles = []
        self._current_link = None
        self._current_name = ''
        super().reset()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and 'profile-link' in attrs.get('class', '').split():
            self._current_link = attrs['href']
            log.debug('Profile link %s open', self._current_link)

    def handle_endtag(self, tag):
        if tag == 'a' and self._current_link:
            log.debug('Profile link %s close', self._current_link)
            self.profiles.append((self._current_name.strip(), self._current_link))
            self._current_link = None
            self._current_name = ''

    def handle_data(self, data):
        if self._current_link:
            self._current_name += data


class CaptureParser(HTMLParser):
    """
    Capture data inside a set of tags, chosen according to given criteria.

    Subclass and define self.criteria
    """

    @staticmethod
    def criteria(tag, attrs):
        raise NotImplementedError()

    def reset(self):
        self.strings = []
        self._current = ''
        self._inside = 0
        super().reset()

    def handle_starttag(self, tag, attrs):
        if self._inside > 0:
            self._inside += 1
        else:
            attrs = dict(attrs)
            if self.criteria(tag, attrs):
                self._inside += 1
                log.debug('%s %s open', self.__class__.__name__[:-6], tag)

    def handle_endtag(self, tag):
        if self._inside > 0:
            self._inside -= 1
            if self._inside == 0:
                log.debug('%s %s close', self.__class__.__name__[:-6], tag)
                self.strings.append(self._current)
                self._current = ''

    def handle_data(self, data):
        if self._inside > 0:
            log.debug('Capture  %r', data)
            self._current += data


class ErrorMessageParser(CaptureParser):
    """Find error messages on page."""

    @staticmethod
    def criteria(tag, attrs):
        return 'ui-message-error' in attrs.get('class', '').split()


class TitleParser(CaptureParser):
    """Find genre title on genre page."""

    @staticmethod
    def criteria(tag, attrs):
        return 'genreTitle' in attrs.get('class', '').split()


class Scraper(object):
    """
    The scraping engine.

    Initialize with credentials, then run Scraper.genre_scan to scrape genre
    pages.
    """

    base_url = 'https://www.netflix.com/'
    login_path = '/login'

    genre_cache_fn = os.path.join(os.path.dirname(__file__), '.genrecache')

    def __init__(self, auth, profile=None):
        self.auth = auth
        self.profile = profile
        self.session = Session()

    def is_login(self, url):
        parsed_url = urlparse(url)
        return parsed_url.path.lower().endswith(self.login_path.lower())

    def login_if_required(self, response):
        if not self.is_login(response.url):
            return response

        form_parser = FormParser()
        form_parser.feed(response.text)
        form_parser.close()
        forms = form_parser.form_data
        for form in forms.values():
            if form['fields'].get('action') == 'loginAction':
                log.debug('Login form: %r', form)
                url = urljoin(response.url, form['attrs']['action'])
                data = dict(form['fields'])
                data.update({'email': self.auth[0], 'password': self.auth[1]})
                response = self.session.request(form['attrs']['method'], url, data=data)
                response.raise_for_status()
                if self.is_login(response.url):
                    error_parser = ErrorMessageParser()
                    error_parser.feed(response.text)
                    error_parser.close()
                    raise RuntimeError(error_parser.strings[1])  # 0 is javascript warning
                else:
                    return response

    def choose_profile_if_required(self, response):
        profile_list_parser = ProfileListParser()
        profile_list_parser.feed(response.text)
        profile_list_parser.close()
        profiles = profile_list_parser.profiles
        names = []
        for name, path in profiles:
            names.append(name)
            if self.profile is None or name.lower() == self.profile.lower():
                url = urljoin(response.url, path)
                log.debug('Choose profile %s (%s)', name, url)
                response = self.session.get(url)
                response.raise_for_status()
                break
        else:
            if names:
                raise ValueError('Profile {} not found in {}'.format(self.profile, names))

        return response

    def get(self, path):
        """Get an arbitrary page, logging in as necessary, return response."""
        url = urljoin(self.base_url, path)
        response = self.session.get(url)
        response.raise_for_status()
        return self.choose_profile_if_required(
            self.login_if_required(
                response
            )
        )

    def login(self):
        """Perform login."""
        log.info('Login')
        return self.get(self.login_path)

    def genre_scan(self, min=1, max=100000, fresh=False):
        """
        Scan for genres.

        min and max define range of genre numbers to scan for.
        Returns an iterator of (genre number, genre title, url) for each genre
        found.

        This scan creates and uses a cache to store previously-found genres and
        avoid making unnecessary network requests. If you want to refresh the
        cache, set fresh=True.
        """
        with shelve.open(self.genre_cache_fn) as genre_cache:
            if fresh:
                genre_cache.clear()

            for number in range(min, max):
                cache_key = str(number)
                try:
                    value = genre_cache[cache_key]  # shelf needs a string key
                except Exception as exc:
                    if not isinstance(exc, KeyError):
                        log.exception('Error retreiving %s from cache')

                    path = '/browse/genre/{}'.format(number)
                    try:
                        response = self.get(path)
                    except Exception:
                        log.warning('GET %s error', path, exc_info=True)
                        continue

                    title_parser = TitleParser()
                    title_parser.feed(response.text)
                    title_parser.close()
                    if len(title_parser.strings) > 0:
                        title = title_parser.strings[0]
                        log.info('Genre %d %s', number, title)
                        genre_cache[cache_key] = (title, response.url)
                        yield number, title, response.url
                    else:
                        genre_cache[cache_key] = None
                else:
                    log.debug('Found %s in cache: %r', cache_key, value)
                    if value:
                        log.info('Genre %d %s [cached]', number, value[0])
                        yield (number,) + value


def main():
    """Entrypoint to genre scraper script."""
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('-e', '--email')
    arg_parser.add_argument('-p', '--password')
    arg_parser.add_argument('-P', '--profile')
    arg_parser.add_argument('-v', '--verbose', action='count', default=0)
    arg_parser.add_argument('--fresh', action='store_true', default=False)
    arg_parser.add_argument('min', type=int, default=1)
    arg_parser.add_argument('max', type=int, default=5000)
    ns = arg_parser.parse_args()

    if ns.email:
        email = ns.email
    else:
        print('Email:', end=' ', file=sys.stderr)
        email = input()
    password = ns.password or getpass()
    profile = ns.profile
    log_level = logging.WARNING - 10 * ns.verbose

    logging.basicConfig(level=log_level, **LOG_FMT)
    scraper = Scraper((email, password), profile=profile)
    # preempt login to raise errors early
    scraper.login()
    started = datetime.now(timezone.utc)
    scan = scraper.genre_scan(ns.min, ns.max, fresh=ns.fresh)
    print('# Genres {}–{}'.format(ns.min, ns.max))
    print('')
    try:
        for number, name, url in scan:
            print('* {} ([#{}]({}))'.format(name, number, url))

    except (KeyboardInterrupt, SystemExit):
        log.warning('Scan interrupted.')
    finally:
        print('')
        print('_Generated on {:%B %d %Y %H:%M:%S %Z}_'.format(started))


if __name__ == '__main__':
    main()
