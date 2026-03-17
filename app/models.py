"""
Pydantic Models
"""
from typing import Optional
from pydantic import BaseModel


# --- Downloads ---
class DownloadRequest(BaseModel):
    url: str
    headers: Optional[dict] = None
    filename: Optional[str] = None
    connections: Optional[int] = None
    curl_command: Optional[str] = None
    engine: Optional[str] = "auto"


# --- Files ---
class RenameRequest(BaseModel):
    new_name: str


class BulkDeleteRequest(BaseModel):
    filenames: list[str]


class CreateFolderRequest(BaseModel):
    name: str


class MoveRequest(BaseModel):
    destination: str


class CreateFileRequest(BaseModel):
    filename: str
    content: Optional[str] = ""


class CompressRequest(BaseModel):
    filenames: list[str]
    archive_name: str
    format: str = "zip"  # zip, tar.gz


class ExtractRequest(BaseModel):
    delete_after: bool = False


class TagsRequest(BaseModel):
    tags: list[str]


class DescriptionRequest(BaseModel):
    description: str


# --- Auth ---
class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"
    quota_bytes: int = 0


# --- Share ---
class ShareLinkRequest(BaseModel):
    filepath: str
    password: Optional[str] = None
    expire_hours: Optional[int] = 24
    max_downloads: int = 0
