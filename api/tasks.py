from datetime import timedelta
import os
import os.path
import pathlib

from django.core.files import File
from django.core.files.base import ContentFile
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from huey import crontab
from huey.contrib.djhuey import task, periodic_task

from .models import TaskStatus, DynamicMix, StaticMix
from .separate import SpleeterSeparator
from .youtubedl import *

@periodic_task(crontab(minute='*/30'))
def check_in_progress_tasks():
    """Periodic task that checks for stale separation tasks and marks them as erroneous."""
    time_threshold = timezone.now() - timedelta(
        minutes=settings.STALE_TASK_MIN_THRESHOLD)
    in_progress_static_mixes = StaticMix.objects.filter(
        status=TaskStatus.IN_PROGRESS, date_created__lte=time_threshold)
    in_progress_dynamic_mixes = DynamicMix.objects.filter(
        status=TaskStatus.IN_PROGRESS, date_created__lte=time_threshold)
    in_progress_static_mixes.update(status=TaskStatus.ERROR,
                                    error='Operation timed out')
    in_progress_dynamic_mixes.update(status=TaskStatus.ERROR,
                                     error='Operation timed out')

@task()
def create_static_mix(static_mix):
    """
    Task to create static mix by first using Spleeter to separate the requested parts
    and then mixing them into a single track.

    :param static_mix: The audio track model (StaticMix) to be processed
    """
    # Mark as in progress
    static_mix.status = TaskStatus.IN_PROGRESS
    static_mix.save()
    try:
        # Get paths
        directory = os.path.join(settings.MEDIA_ROOT, settings.SEPARATE_DIR,
                                 str(static_mix.id))
        filename = slugify(static_mix.formatted_name()) + '.mp3'
        rel_media_path = os.path.join(settings.SEPARATE_DIR,
                                      str(static_mix.id), filename)
        rel_path = os.path.join(settings.MEDIA_ROOT, rel_media_path)
        rel_path_dir = os.path.join(settings.MEDIA_ROOT, settings.SEPARATE_DIR,
                                    str(static_mix.id))

        pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
        separator = SpleeterSeparator()

        parts = {
            'vocals': static_mix.vocals,
            'drums': static_mix.drums,
            'bass': static_mix.bass,
            'other': static_mix.other
        }

        # Non-local filesystems like S3/Azure Blob do not support source_path()
        is_local = settings.DEFAULT_FILE_STORAGE == 'django.core.files.storage.FileSystemStorage'
        path = static_mix.source_path() if is_local else static_mix.source_url(
        )
        separator.create_static_mix(parts, path, rel_path)

        # Check file exists
        if os.path.exists(rel_path):
            static_mix.status = TaskStatus.DONE
            if is_local:
                # File is already on local filesystem
                static_mix.file.name = rel_media_path
            else:
                # Need to copy local file to S3/Azure Blob/etc.
                raw_file = open(rel_path, 'rb')
                content_file = ContentFile(raw_file.read())
                content_file.name = filename
                static_mix.file = content_file
                # Remove local file
                os.remove(rel_path)
                # Remove empty directory
                os.rmdir(rel_path_dir)
            static_mix.save()
        else:
            raise Exception('Error writing to file')
    except FileNotFoundError as error:
        print(error)
        print('Please make sure you have FFmpeg and FFprobe installed.')
        static_mix.status = TaskStatus.ERROR
        static_mix.error = str(error)
        static_mix.save()
    except Exception as error:
        print(error)
        static_mix.status = TaskStatus.ERROR
        static_mix.error = str(error)
        static_mix.save()

@task()
def create_dynamic_mix(dynamic_mix):
    """
    Task to create dynamic mix by using Spleeter to separate the track into
    vocals, accompaniment, bass, and drum parts.

    :param dynamic_mix: The audio track model (StaticMix) to be processed
    """
    # Mark as in progress
    dynamic_mix.status = TaskStatus.IN_PROGRESS
    dynamic_mix.save()
    try:
        # Get paths
        directory = os.path.join(settings.MEDIA_ROOT, settings.SEPARATE_DIR,
                                 str(dynamic_mix.id))
        rel_media_path = os.path.join(settings.SEPARATE_DIR,
                                      str(dynamic_mix.id))
        rel_media_path_vocals = os.path.join(rel_media_path, 'vocals.mp3')
        rel_media_path_other = os.path.join(rel_media_path, 'other.mp3')
        rel_media_path_bass = os.path.join(rel_media_path, 'bass.mp3')
        rel_media_path_drums = os.path.join(rel_media_path, 'drums.mp3')
        rel_path = os.path.join(settings.MEDIA_ROOT, rel_media_path)

        pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
        separator = SpleeterSeparator()

        # Non-local filesystems like S3/Azure Blob do not support source_path()
        is_local = settings.DEFAULT_FILE_STORAGE == 'django.core.files.storage.FileSystemStorage'
        path = dynamic_mix.source_path(
        ) if is_local else dynamic_mix.source_url()
        separator.separate_into_parts(path, rel_path)

        # Check all parts exist
        if exists_all_parts(rel_path):
            dynamic_mix.status = TaskStatus.DONE
            if is_local:
                # File is already on local filesystem
                dynamic_mix.vocals_file.name = rel_media_path_vocals
                dynamic_mix.other_file.name = rel_media_path_other
                dynamic_mix.bass_file.name = rel_media_path_bass
                dynamic_mix.drums_file.name = rel_media_path_drums
            else:
                save_to_ext_storage(dynamic_mix, rel_path)
            dynamic_mix.save()
        else:
            raise Exception('Error writing to file')
    except FileNotFoundError as error:
        print(error)
        print('Please make sure you have FFmpeg and FFprobe installed.')
        dynamic_mix.status = TaskStatus.ERROR
        dynamic_mix.error = str(error)
        dynamic_mix.save()
    except Exception as error:
        print(error)
        dynamic_mix.status = TaskStatus.ERROR
        dynamic_mix.error = str(error)
        dynamic_mix.save()

@task(retries=settings.YOUTUBE_MAX_RETRIES)
def fetch_youtube_audio(source_file, artist, title, link):
    """
    Task that uses youtubedl to extract the audio from a YouTube link.

    :param source_file: SourceFile model
    :param artist: Track artist
    :param title: Track title
    :param link: YouTube link
    """
    fetch_task = source_file.youtube_fetch_task
    # Mark as in progress
    fetch_task.status = TaskStatus.IN_PROGRESS
    fetch_task.save()

    try:
        # Get paths
        directory = os.path.join(settings.MEDIA_ROOT, settings.UPLOAD_DIR,
                                 str(source_file.id))
        filename = slugify(artist + ' - ' + title,
                           allow_unicode=True) + get_file_ext(link)
        rel_media_path = os.path.join(settings.UPLOAD_DIR, str(fetch_task.id),
                                      filename)
        rel_path = os.path.join(settings.MEDIA_ROOT, rel_media_path)
        pathlib.Path(directory).mkdir(parents=True, exist_ok=True)

        # Start download
        download_audio(link, rel_path)

        is_local = settings.DEFAULT_FILE_STORAGE == 'django.core.files.storage.FileSystemStorage'

        # Check file exists
        if os.path.exists(rel_path):
            fetch_task.status = TaskStatus.DONE
            if is_local:
                # File is already on local filesystem
                source_file.file.name = rel_media_path
            else:
                # Need to copy local file to S3/Azure Blob/etc.
                raw_file = open(rel_path, 'rb')
                content_file = ContentFile(raw_file.read())
                content_file.name = filename
                source_file.file = content_file
                rel_dir_path = os.path.join(settings.MEDIA_ROOT,
                                            settings.UPLOAD_DIR,
                                            str(source_file.id))
                # Remove local file
                os.remove(rel_path)
                # Remove empty directory
                os.rmdir(rel_dir_path)
            fetch_task.save()
            source_file.save()
        else:
            raise Exception('Error writing to file')
    except Exception as error:
        print(error)
        fetch_task.status = TaskStatus.ERROR
        fetch_task.error = str(error)
        fetch_task.save()
        raise error

def exists_all_parts(rel_path):
    """Returns whether all of the individual parts exist on filesystem."""
    rel_path_vocals = os.path.join(rel_path, 'vocals.mp3')
    rel_path_other = os.path.join(rel_path, 'other.mp3')
    rel_path_bass = os.path.join(rel_path, 'bass.mp3')
    rel_path_drums = os.path.join(rel_path, 'drums.mp3')
    return os.path.exists(rel_path_vocals) and os.path.exists(
        rel_path_other) and os.path.exists(rel_path_bass) and os.path.exists(
            rel_path_drums)

def save_to_ext_storage(dynamic_mix, rel_path_dir):
    """Saves individual parts to external file storage (S3, Azure, etc.)

    :param dynamic_mix: DynamicMix model
    :param rel_path_dir: Relative path to DynamicMix ID directory
    """
    filenames = ['vocals.mp3', 'other.mp3', 'bass.mp3', 'drums.mp3']
    for filename in filenames:
        rel_path = os.path.join(rel_path_dir, filename)
        raw_file = open(rel_path, 'rb')
        content_file = ContentFile(raw_file.read())
        content_file.name = filename

        if filename == 'vocals.mp3':
            dynamic_mix.vocals_file = content_file
        elif filename == 'other.mp3':
            dynamic_mix.other_file = content_file
        elif filename == 'bass.mp3':
            dynamic_mix.bass_file = content_file
        elif filename == 'drums.mp3':
            dynamic_mix.drums_file = content_file

        # Remove local file
        os.remove(rel_path)
    # Remove empty directory
    os.rmdir(rel_path_dir)
