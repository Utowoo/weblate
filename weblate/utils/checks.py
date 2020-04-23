#
# Copyright © 2012 - 2020 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#


import errno
import os
import sys
import time
from collections import defaultdict
from itertools import chain

from celery.exceptions import TimeoutError
from django.conf import settings
from django.core.cache import cache
from django.core.checks import Critical, Error, Info
from django.core.mail import get_connection

from weblate import settings_example
from weblate.utils.celery import get_queue_stats
from weblate.utils.data import data_dir
from weblate.utils.docs import get_doc_url
from weblate.utils.tasks import ping

GOOD_CACHE = {"MemcachedCache", "PyLibMCCache", "DatabaseCache", "RedisCache"}
DEFAULT_MAILS = {
    "root@localhost",
    "webmaster@localhost",
    "noreply@weblate.org",
    "noreply@example.com",
}
DOC_LINKS = {
    "security.W001": ("admin/upgdade", "up-3-1"),
    "security.W002": ("admin/upgdade", "up-3-1"),
    "security.W003": ("admin/upgdade", "up-3-1"),
    "security.W004": ("admin/install", "production-ssl"),
    "security.W005": ("admin/install", "production-ssl"),
    "security.W006": ("admin/upgdade", "up-3-1"),
    "security.W007": ("admin/upgdade", "up-3-1"),
    "security.W008": ("admin/install", "production-ssl"),
    "security.W009": ("admin/install", "production-secret"),
    "security.W010": ("admin/install", "production-ssl"),
    "security.W011": ("admin/install", "production-ssl"),
    "security.W012": ("admin/install", "production-ssl"),
    "security.W018": ("admin/install", "production-debug"),
    "security.W019": ("admin/upgdade", "up-3-1"),
    "security.W020": ("admin/install", "production-hosts"),
    "security.W021": ("admin/install", "production-ssl"),
    "weblate.E002": ("admin/install", "file-permissions"),
    "weblate.E003": ("admin/install", "out-mail"),
    "weblate.E005": ("admin/install", "celery"),
    "weblate.E006": ("admin/install", "production-database"),
    "weblate.E007": ("admin/install", "production-cache"),
    "weblate.E008": ("admin/install", "production-cache-avatar"),
    "weblate.E009": ("admin/install", "celery"),
    "weblate.E011": ("admin/install", "production-admins"),
    "weblate.E012": ("admin/install", "production-email"),
    "weblate.E013": ("admin/install", "production-email"),
    "weblate.E014": ("admin/install", "production-secret"),
    "weblate.E015": ("admin/install", "production-hosts"),
    "weblate.E016": ("admin/install", "production-templates"),
    "weblate.E017": ("admin/install", "production-site"),
    "weblate.E018": ("admin/optionals", "avatars"),
    "weblate.E019": ("admin/install", "celery"),
    "weblate.E020": ("admin/install", "celery"),
    "weblate.I021": ("admin/install", "collecting-errors"),
    "weblate.E022": ("admin/optionals", "git-exporter"),
    "weblate.C023": ("admin/install", "production-encoding"),
    "weblate.C024": ("admin/install", "pangocairo"),
    "weblate.W025": ("admin/install", "optional-deps"),
    "weblate.E026": ("admin/install", "celery"),
    "weblate.E027": ("admin/install", "file-permissions"),
    "weblate.I028": ("admin/backup",),
    "weblate.C029": ("admin/backup",),
    "weblate.C030": ("admin/install", "celery"),
    "weblate.I031": ("admin/upgrade",),
    "weblate.C031": ("admin/upgrade",),
    "weblate.C032": ("admin/install",),
}


def weblate_check(id, message, cls=Critical):
    """Returns Django check instance."""
    docid = id
    while docid.count(".") > 1:
        docid = docid.rsplit(".", 1)[0]
    return cls(message, hint=get_doc_url(*DOC_LINKS[docid]), id=id)


def check_mail_connection(app_configs, **kwargs):
    errors = []
    try:
        connection = get_connection()
        connection.open()
        connection.close()
    except Exception as error:
        message = "Can not send email ({}), please check EMAIL_* settings."
        errors.append(weblate_check(message.format(error), "weblate.E003"))

    return errors


def is_celery_queue_long():
    """
    Checks whether celery queue is too long.

    It does trigger if it is too long for at least one hour. This way we filter out
    peaks and avoid flipping warning on big operations (eg. site-wide autotranslate).
    """
    cache_key = "celery_queue_stats"
    queues_data = cache.get(cache_key, {})

    # Hours since epoch
    current_hour = int(time.time() / 3600)
    test_hour = current_hour - 1

    # Fetch current stats
    stats = get_queue_stats()

    # Update counters
    if current_hour not in queues_data:
        # Delete stale items
        for key in list(queues_data.keys()):
            if key < test_hour:
                del queues_data[key]
        # Add current one
        queues_data[current_hour] = stats

        # Store to cache
        cache.set(cache_key, queues_data, 7200)

    # Do not fire if we do not have counts for two hours ago
    if test_hour not in queues_data:
        return False

    # Check if any queue got bigger
    base = queues_data[test_hour]
    thresholds = defaultdict(lambda: 50)
    thresholds["translate"] = 1000
    return any(
        stat > thresholds[key] and base.get(key, 0) > thresholds[key]
        for key, stat in stats.items()
    )


def check_celery(app_configs, **kwargs):
    errors = []
    if settings.CELERY_TASK_ALWAYS_EAGER:
        errors.append(
            weblate_check(
                "weblate.E005", "Celery is configured in the eager mode", Error
            )
        )
    elif settings.CELERY_BROKER_URL == "memory://":
        errors.append(
            weblate_check(
                "weblate.E026", "Celery is configured to store queue in local memory"
            )
        )
    else:
        if is_celery_queue_long():
            errors.append(
                weblate_check(
                    "weblate.E009",
                    "The Celery tasks queue is too long, either the worker "
                    "is not running or is too slow.",
                )
            )

        result = ping.delay()
        try:
            result.get(timeout=10, disable_sync_subtasks=False)
        except TimeoutError:
            errors.append(
                weblate_check(
                    "weblate.E019",
                    "The Celery does not process tasks or is too slow "
                    "in processing them.",
                )
            )
        except NotImplementedError:
            errors.append(
                weblate_check(
                    "weblate.E020",
                    "The Celery is not configured to store results, "
                    "CELERY_RESULT_BACKEND is probably not set.",
                )
            )
    heartbeat = cache.get("celery_heartbeat")
    loaded = cache.get("celery_loaded")
    now = time.time()
    if loaded and now - loaded > 60 and (not heartbeat or now - heartbeat > 600):
        errors.append(
            weblate_check(
                "weblate.C030",
                "The Celery beats scheduler is not executing periodic tasks "
                "in a timely manner.",
            )
        )

    return errors


def check_database(app_configs, **kwargs):
    if settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
        return []
    return [
        weblate_check(
            "weblate.E006",
            "Please migrate your database to use PostgreSQL. "
            "Support for other database backends will be dropped in Weblate 4.0 "
            "currently sheduled on April 2020.",
            Error,
        )
    ]


def check_cache(app_configs, **kwargs):
    """Check for sane caching."""
    errors = []

    cache_backend = settings.CACHES["default"]["BACKEND"].split(".")[-1]
    if cache_backend not in GOOD_CACHE:
        errors.append(
            weblate_check(
                "weblate.E007",
                "The configured cache backend will lead to serious "
                "performance or consistency issues.",
            )
        )

    if settings.ENABLE_AVATARS and "avatar" not in settings.CACHES:
        errors.append(
            weblate_check(
                "weblate.E008",
                "Please configure separate avatar caching to reduce pressure "
                "on the default cache",
                Error,
            )
        )

    return errors


def check_settings(app_configs, **kwargs):
    """Check for sane settings."""
    errors = []

    if not settings.ADMINS or "noreply@weblate.org" in (x[1] for x in settings.ADMINS):
        errors.append(
            weblate_check(
                "weblate.E011", "The site admins seem to be wrongly configured", Error
            )
        )

    if settings.SERVER_EMAIL in DEFAULT_MAILS:
        errors.append(
            weblate_check("weblate.E012", "The server email has default value")
        )
    if settings.DEFAULT_FROM_EMAIL in DEFAULT_MAILS:
        errors.append(
            weblate_check("weblate.E013", "The default from email has default value")
        )

    if settings.SECRET_KEY == settings_example.SECRET_KEY:
        errors.append(
            weblate_check("weblate.E014", "The cookie secret key has default value")
        )

    if not settings.ALLOWED_HOSTS:
        errors.append(
            weblate_check("weblate.E015", "The allowed hosts are not configured")
        )
    return errors


def check_templates(app_configs, **kwargs):
    """Check for cached DjangoTemplates Loader."""
    if settings.DEBUG:
        return []

    from django.template import engines
    from django.template.backends.django import DjangoTemplates
    from django.template.loaders import cached

    is_cached = True

    for engine in engines.all():
        if not isinstance(engine, DjangoTemplates):
            continue

        for loader in engine.engine.template_loaders:
            if not isinstance(loader, cached.Loader):
                is_cached = False

    if is_cached:
        return []

    return [
        weblate_check(
            "weblate.E016",
            "Configure cached template loader for better performance",
            Error,
        )
    ]


def check_data_writable(app_configs=None, **kwargs):
    """Check we can write to data dir."""
    errors = []
    dirs = [
        settings.DATA_DIR,
        data_dir("home"),
        data_dir("ssh"),
        data_dir("vcs"),
        data_dir("celery"),
        data_dir("backups"),
        data_dir("fonts"),
        data_dir("cache", "fonts"),
    ]
    message = "Path {} is not writable, check your DATA_DIR settings."
    for path in dirs:
        if not os.path.exists(path):
            os.makedirs(path)
        elif not os.access(path, os.W_OK):
            errors.append(weblate_check("weblate.E002", message.format(path)))

    return errors


def check_site(app_configs, **kwargs):
    from weblate.utils.site import get_site_domain, check_domain

    errors = []
    if not check_domain(get_site_domain()):
        errors.append(weblate_check("weblate.E017", "Configure correct site domain"))
    return errors


def check_perms(app_configs=None, **kwargs):
    """Check we can write to data dir."""
    errors = []
    uid = os.getuid()
    message = "Path {} is owned by different user, check your DATA_DIR settings."
    for dirpath, dirnames, filenames in os.walk(settings.DATA_DIR):
        for name in chain(dirnames, filenames):
            path = os.path.join(dirpath, name)
            try:
                stat = os.lstat(path)
            except OSError as error:
                # File was removed meanwhile
                if error.errno == errno.ENOENT:
                    continue
                raise
            if stat.st_uid != uid:
                errors.append(weblate_check("weblate.E027", message.format(path)))

    return errors


def check_errors(app_configs=None, **kwargs):
    """Check there is error collection configured."""
    if (
        hasattr(settings, "ROLLBAR")
        or hasattr(settings, "RAVEN_CONFIG")
        or settings.SENTRY_DSN
    ):
        return []
    return [
        weblate_check(
            "weblate.I021",
            "Error collection is not configured, "
            "it is highly recommended for production use",
            Info,
        )
    ]


def check_encoding(app_configs=None, **kwargs):
    """Check there is encoding is utf-8."""
    if sys.getfilesystemencoding() == "utf-8" and sys.getdefaultencoding() == "utf-8":
        return []
    return [
        weblate_check(
            "weblate.C023",
            "System encoding is not utf-8, processing non-ASCII strings will break",
        )
    ]


def check_diskspace(app_configs=None, **kwargs):
    """Check free disk space."""
    stat = os.statvfs(settings.DATA_DIR)
    if stat.f_bavail * stat.f_bsize < 10000000:
        return [weblate_check("weblate.C032", "The disk is nearly full")]
    return []
