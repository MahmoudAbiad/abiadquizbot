import asyncio
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
import uuid
from unittest.mock import AsyncMock, Mock

from reliability_support import load_functions
from services.upload_validation import (
    DOCUMENT_EXTENSIONS, TELEGRAM_IMAGE_DOCUMENT_EXTENSIONS,
    supported_extension, validate_image,
)


class ImageDocumentRoutingTests(unittest.IsolatedAsyncioTestCase):
    def env(self):
        return load_functions(
            'handlers/files.py', {'handle_media'}, os=os, asyncio=asyncio,
            json=json, uuid=uuid, DOWNLOADS_DIR='downloads',
            TELEGRAM_IMAGE_DOCUMENT_EXTENSIONS=TELEGRAM_IMAGE_DOCUMENT_EXTENSIONS,
            DOCUMENT_EXTENSIONS=DOCUMENT_EXTENSIONS,
            QuizState=types.SimpleNamespace(processing_file_quiz='processing'),
            PENDING_REQUEST_STATES=(), offer_transition=AsyncMock(return_value=False),
            message_action=Mock(return_value={}), ensure_directory_exists=Mock(),
            download_photos_service=AsyncMock(return_value=(['source.png'], '')),
            compute_combined_hash=Mock(return_value='image-hash'),
            _finalize_media_processing=AsyncMock(), safe_file_cleanup=Mock(),
            validate_file_size=Mock(return_value=(True, '')),
            supported_extension=supported_extension,
            bot=types.SimpleNamespace(download=AsyncMock()),
            validate_document=Mock(return_value=1), calculate_file_hash=Mock(return_value='doc-hash'),
            _document_content_source=Mock(return_value='txt'),
            logger=Mock(), log_error=Mock(),
            redis_client=types.SimpleNamespace(rpush=AsyncMock(), expire=AsyncMock(), set=AsyncMock(return_value=True)),
            process_album_background=Mock(return_value='album-task'), spawn=Mock(),
        )

    def message(self, name):
        return types.SimpleNamespace(
            document=types.SimpleNamespace(file_name=name, file_id='file', file_unique_id='unique', file_size=100),
            photo=None, media_group_id=None, from_user=types.SimpleNamespace(id=1),
            chat=types.SimpleNamespace(id=1), answer=AsyncMock(),
        )

    async def test_image_documents_use_image_pipeline_and_hash(self):
        for name in ('scan.png', 'scan.jpg', 'scan.JPEG', 'scan.PNG'):
            with self.subTest(name=name):
                env = self.env()
                message = self.message(name)
                await env['handle_media'](message, types.SimpleNamespace(get_state=AsyncMock(return_value=None)))
                image = env['download_photos_service'].await_args.args[1][0]
                self.assertEqual(image['extension'], os.path.splitext(name)[1].lower())
                self.assertEqual(env['_finalize_media_processing'].await_args.kwargs['content_source'], 'images')
                env['compute_combined_hash'].assert_called_once_with(['source.png'])
                env['validate_document'].assert_not_called()
                env['log_error'].assert_not_called()

    async def test_document_images_collect_into_album(self):
        env = self.env()
        message = self.message('page.PNG')
        message.media_group_id = 'album'
        await env['handle_media'](message, types.SimpleNamespace(get_state=AsyncMock(return_value=None)))
        payload = json.loads(env['redis_client'].rpush.await_args.args[1])
        self.assertEqual(payload['extension'], '.png')
        env['spawn'].assert_called_once_with('album-task')
        env['download_photos_service'].assert_not_awaited()

    async def test_photo_keeps_existing_jpeg_path(self):
        env = self.env()
        message = self.message('unused')
        message.photo, message.document = [message.document], None
        await env['handle_media'](message, types.SimpleNamespace(get_state=AsyncMock(return_value=None)))
        self.assertEqual(env['download_photos_service'].await_args.args[1][0]['extension'], '.jpg')

    async def test_oversize_image_reports_error_without_finalizing(self):
        env = self.env()
        env['download_photos_service'].return_value = ([], 'image too large')
        message = self.message('scan.png')
        await env['handle_media'](message, types.SimpleNamespace(get_state=AsyncMock(return_value=None)))
        message.answer.assert_awaited_once_with('image too large')
        env['_finalize_media_processing'].assert_not_awaited()

    async def test_text_document_keeps_document_path(self):
        env = self.env()
        await env['handle_media'](self.message('notes.txt'), types.SimpleNamespace(get_state=AsyncMock(return_value=None)))
        env['validate_document'].assert_called_once()
        env['download_photos_service'].assert_not_awaited()
        self.assertEqual(env['_finalize_media_processing'].await_args.kwargs['content_source'], 'txt')
        env['log_error'].assert_not_called()


class ImageDocumentDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_bytes_and_extension_preserved_and_bad_images_cleaned(self):
        from PIL import Image
        import io
        with tempfile.TemporaryDirectory() as directory:
            payloads = {}
            for ext, fmt in (('.png', 'PNG'), ('.jpg', 'JPEG'), ('.jpeg', 'JPEG')):
                buffer = io.BytesIO()
                Image.new('RGB', (20, 20), 'white').save(buffer, fmt)
                payloads[ext] = buffer.getvalue()
            payloads['broken'] = b'not an image'
            async def download(file_id, destination):
                Path(destination).write_bytes(payloads[file_id])
            env = load_functions(
                'services/file_service.py', {'download_photos_service'},
                asyncio=asyncio, os=os, uuid=uuid, DOWNLOADS_DIR=directory,
                bot=types.SimpleNamespace(download=download),
                validate_file_size=Mock(return_value=(True, '')),
                validate_image=validate_image, supported_extension=supported_extension,
                TELEGRAM_IMAGE_DOCUMENT_EXTENSIONS=TELEGRAM_IMAGE_DOCUMENT_EXTENSIONS,
                safe_file_cleanup=lambda path: Path(path).unlink(missing_ok=True),
            )
            for ext in ('.png', '.jpg', '.jpeg'):
                paths, error = await env['download_photos_service'](1, [{'file_id': ext, 'extension': ext, 'file_size': 100}])
                self.assertEqual(error, '')
                self.assertTrue(paths[0].endswith(ext))
                self.assertEqual(Path(paths[0]).read_bytes(), payloads[ext])
                Path(paths[0]).unlink()
            with self.assertRaises(OSError):
                await env['download_photos_service'](1, [
                    {'file_id': '.png', 'extension': '.png'},
                    {'file_id': 'broken', 'extension': '.png'},
                ])
            self.assertEqual(list(Path(directory).iterdir()), [])
            env['validate_file_size'].return_value = (False, 'too large')
            paths, error = await env['download_photos_service'](1, [{'file_id': '.png'}])
            self.assertEqual((paths, error), ([], '❌ الصورة رقم 1: too large'))
            self.assertEqual(list(Path(directory).iterdir()), [])
