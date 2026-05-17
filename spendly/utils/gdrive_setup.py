"""Google Drive OAuth2 Setup Tool.

Run this script locally to authorize Spendly and generate token.json.
Usage: uv run python spendly/utils/gdrive_setup.py
"""

from __future__ import annotations

import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from spendly.core.config import settings


def main():
    print("--- Google Drive OAuth2 Setup ---")

    client_path = settings.gdrive_oauth_client_path
    token_path = settings.gdrive_oauth_token_path

    if not client_path or not os.path.exists(client_path):
        print(f"ERROR: Client configuration JSON not found at {client_path}")
        print("Please download your OAuth 2.0 Client ID JSON from GCP Console.")
        return

    # Scopes required for Drive file management.
    # https://www.googleapis.com/auth/drive.file
    scopes = ["https://www.googleapis.com/auth/drive.file"]

    try:
        print(f"Using client config: {client_path}")
        flow = InstalledAppFlow.from_client_secrets_file(client_path, scopes)

        # This will open a browser window for login
        creds = flow.run_local_server(port=0)

        # Save the credentials for the next run
        print(f"Saving token to: {token_path}")
        token_dir = Path(token_path).parent
        token_dir.mkdir(parents=True, exist_ok=True)

        with open(token_path, "w") as token:
            token.write(creds.to_json())

        print("\nSUCCESS! Google Drive integration is now authorized.")
        print("You can now run verify_gdrive.py to test the upload.")

    except Exception as e:
        print(f"\nFAILED: {e}")


if __name__ == "__main__":
    main()
