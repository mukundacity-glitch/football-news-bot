# src/drive_utils.py
import os
import asyncio
from pathlib import Path
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Define the required scopes for Google Drive API access
SCOPES = ['https://www.googleapis.com/auth/drive.file']

def authenticate_drive():
    """Authenticates the Google Service Account safely via environment variables."""
    # For maximum cloud security, we prioritize an environment variable holding the JSON string
    creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    
    try:
        if creds_json:
            import json
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            # Local fallback for development testing (ensure credentials.json is gitignored)
            creds = Credentials.from_service_account_file('credentials.json', scopes=SCOPES)
        
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"  [DRIVE ERROR] Authentication failed. Verify Service Account keys: {e}")
        return None

def _upload_file_sync(service, file_path: Path, folder_id: str):
    """Synchronous file upload to Google Drive."""
    if not service or not file_path.exists():
        print(f"  [DRIVE ERROR] Invalid service or missing local file: {file_path}")
        return False

    try:
        file_metadata = {
            'name': file_path.name,
            'parents': [folder_id]
        }
        
        # Dynamically determine mimetype to ensure Drive parses the card correctly
        mime_type = 'image/png' if file_path.suffix.lower() == '.png' else 'application/json'
        
        media = MediaFileUpload(str(file_path), mimetype=mime_type, resumable=True)
        
        uploaded_file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        print(f"  ✅ [DRIVE] Successfully backed up {file_path.name} (Drive ID: {uploaded_file.get('id')})")
        return True
    except Exception as e:
        print(f"  [DRIVE ERROR] Upload execution failed for {file_path.name}: {e}")
        return False

async def upload_to_drive(file_path: Path, folder_id: str):
    """Asynchronous wrapper to prevent blocking the main asyncio event loop during upload network latency."""
    service = authenticate_drive()
    if not service:
        return False
        
    # Offload the synchronous API network call to a background thread
    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(None, _upload_file_sync, service, file_path, folder_id)
    return success
