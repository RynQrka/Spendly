from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from spendly.core.config import settings
from spendly.core.logger import get_logger

log = get_logger(__name__)

_service = None


def _get_drive_service():
    global _service

    if _service:
        return _service

    if not settings.gdrive_oauth_token_path or not os.path.exists(settings.gdrive_oauth_token_path):
        log.error("Google Drive token not found. Run gdrive_setup.py first.")
        return None

    try:
        if os.path.getsize(settings.gdrive_oauth_token_path) == 0:
            log.error("Google Drive token file is empty. Run gdrive_setup.py first.")
            return None

        creds = Credentials.from_authorized_user_file(
            settings.gdrive_oauth_token_path,
            ["https://www.googleapis.com/auth/drive.file"],
        )

        # Refresh token if expired
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing Google Drive token...")
            creds.refresh(Request())
            # Save the refreshed credentials back to the token file
            with open(settings.gdrive_oauth_token_path, "w") as token:
                token.write(creds.to_json())

        _service = build("drive", "v3", credentials=creds)
        return _service
    except Exception:
        log.error("Failed to initialize Google Drive service", exc_info=True)
        return None


def upload_to_gdrive(file_path: Path, mime_type: str | None = None) -> str | None:
    """Upload a file to Google Drive (OAuth2) and return shareable link."""

    if not settings.gdrive_oauth_token_path or not settings.gdrive_folder_id:
        log.warning("Google Drive integration skipped: missing config")
        return None

    if not file_path.exists():
        log.error(f"File not found: {file_path}")
        return None

    try:
        service = _get_drive_service()
        if not service:
            return None

        mime_type = mime_type or mimetypes.guess_type(file_path)[0] or "application/octet-stream"

        file_metadata = {
            "name": file_path.name,
            "parents": [settings.gdrive_folder_id],
        }

        media = MediaFileUpload(str(file_path), mimetype=mime_type, resumable=True)

        gfile = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )

        file_id = gfile.get("id")
        link = f"https://drive.google.com/file/d/{file_id}/view"

        log.info(
            "File uploaded to Google Drive",
            extra={"file": file_path.name, "file_id": file_id},
        )

        return link

    except Exception:
        log.error(f"Google Drive upload failed for {file_path.name}", exc_info=True)
        return None
