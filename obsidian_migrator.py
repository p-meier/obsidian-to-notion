#!/usr/bin/env python3
"""
Obsidian to Notion Migrator
Single-file migration tool for converting Obsidian vaults to Notion workspaces
"""

import os
import re
import json
import time
import logging
import hashlib
import mimetypes
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote
import yaml

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from notion_client import Client
from tqdm import tqdm

# Constants
DEFAULT_CONFIG = {
    'batch_size': 10,
    'max_workers': 3,
    'max_file_size': 100 * 1024 * 1024,  # 100MB
    'supported_extensions': ['.png', '.jpg', '.jpeg', '.gif', '.pdf', '.mp4', '.mov', '.mp3', '.wav', '.doc', '.docx'],
    'rate_limit_delay': 0.34,  # Notion allows 3 requests per second
    'retry_attempts': 5,
    'timeout': 30,
    'default_database_properties': {
        'Name': {'type': 'title'},
        'Tags': {'type': 'multi_select'},
        'Created': {'type': 'created_time'},
        'Modified': {'type': 'last_edited_time'},
        'Source File': {'type': 'rich_text'}
    }
}

@dataclass
class MigrationConfig:
    notion_token: str
    target_database_id: str
    source_vault_path: str
    attachments_folder: str = "attachments"
    batch_size: int = 10
    max_workers: int = 3
    max_file_size: int = 100 * 1024 * 1024
    supported_extensions: List[str] = None
    dry_run: bool = False
    database_properties: Dict[str, Dict] = None
    extract_frontmatter: bool = True
    target_subfolder: Optional[str] = None
    
    def __post_init__(self):
        if self.supported_extensions is None:
            self.supported_extensions = DEFAULT_CONFIG['supported_extensions']
        if self.database_properties is None:
            self.database_properties = DEFAULT_CONFIG['default_database_properties']

        # Resolve source vault path to be absolute
        if self.source_vault_path:
            self.source_vault_path = str(Path(self.source_vault_path).expanduser().resolve())

@dataclass 
class FileInfo:
    """Information about a file to be uploaded"""
    path: Path
    name: str
    size: int
    mime_type: str
    hash: str
    
@dataclass
class UploadResult:
    """Result of a file upload operation"""
    success: bool
    upload_id: Optional[str] = None
    error_message: Optional[str] = None
    file_path: Optional[str] = None

@dataclass
class MarkdownFile:
    """Information about a markdown file and its metadata"""
    path: Path
    title: str
    content: str
    frontmatter: Dict[str, any]
    file_references: List[Tuple[str, Optional[str]]]


class ObsidianToNotionMigrator:
    def __init__(self, config: MigrationConfig):
        self.config = config
        self.notion = Client(auth=config.notion_token)
        self.logger = self._setup_logging()
        self.uploaded_files: Dict[str, str] = {}  # hash -> upload_id
        self.failed_files: List[str] = []
        self.processed_files: Set[str] = set()
        self.session = self._setup_session()
        
        # Validate configuration
        self._validate_config()
        
    def _setup_logging(self) -> logging.Logger:
        """Configure logging with appropriate levels and formatting"""
        logger = logging.getLogger('obsidian_migrator')
        logger.setLevel(logging.INFO)
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # File handler
        file_handler = logging.FileHandler('migration.log')
        file_handler.setLevel(logging.DEBUG)
        
        # Formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        console_handler.setFormatter(formatter)
        file_handler.setFormatter(formatter)
        
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)
        
        return logger
    
    def _setup_session(self) -> requests.Session:
        """Configure HTTP session with retry logic and timeouts"""
        session = requests.Session()
        
        # Retry strategy for handling transient failures
        retry_strategy = Retry(
            total=self.config.retry_attempts if hasattr(self.config, 'retry_attempts') else 5,
            status_forcelist=[429, 500, 502, 503, 504],
            backoff_factor=1,
            respect_retry_after_header=True
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        
        return session
    
    def _validate_config(self) -> None:
        """Validate configuration parameters"""
        if not self.config.notion_token:
            raise ValueError("Notion API token is required")
        
        if not self.config.target_database_id:
            raise ValueError("Target database ID is required")
        
        vault_path = Path(self.config.source_vault_path)
        if not vault_path.exists():
            raise ValueError(f"Vault path does not exist: {vault_path}")
        
        if not vault_path.is_dir():
            raise ValueError(f"Vault path is not a directory: {vault_path}")
        
        # Validate database exists and is accessible (skip for dry run with fake credentials)
        if not (self.config.dry_run and self.config.notion_token.startswith("fake")):
            try:
                database = self.notion.databases.retrieve(self.config.target_database_id)
                self.logger.info(f"Target database: {database['title'][0]['plain_text']}")
            except Exception as e:
                raise ValueError(f"Cannot access target database: {e}")
        
        self.logger.info("Configuration validated successfully")
    
    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for Notion API upload"""
        # URL decode the filename first
        decoded = unquote(filename)
        
        # Replace problematic characters
        sanitized = decoded.replace('=', '_').replace('$', '_').replace('?', '_')
        sanitized = sanitized.replace('&', '_and_').replace('%', '_percent_')
        sanitized = sanitized.replace('#', '_hash_').replace('+', '_plus_')
        
        # Remove or replace other special characters that might cause issues
        sanitized = re.sub(r'[<>:"|*]', '_', sanitized)
        
        # Collapse multiple underscores
        sanitized = re.sub(r'_+', '_', sanitized)
        
        # Remove leading/trailing underscores
        sanitized = sanitized.strip('_')
        
        return sanitized
    
    def _normalize_code_language(self, language: str) -> str:
        """Normalize code language to supported Notion languages"""
        if not language:
            return "plain text"
        
        # Map common aliases and unsupported languages to supported ones
        language_map = {
            'cardlink': 'plain text',
            'text': 'plain text',
            'txt': 'plain text',
            'py': 'python',
            'js': 'javascript',
            'ts': 'typescript',
            'jsx': 'javascript',
            'tsx': 'typescript',
            'md': 'markdown',
            'yml': 'yaml',
            'sh': 'shell',
            'bash': 'shell',
            'zsh': 'shell',
            'fish': 'shell'
        }
        
        normalized = language.lower().strip()
        return language_map.get(normalized, normalized if normalized else "plain text")
    
    def _parse_list_item_content(self, text: str, asset_mapping: Dict[str, str]) -> Dict:
        """Parse list item content that may contain both text and embeds"""
        # Check if the text contains embeds
        if '![[' in text:
            # For mixed content, extract the text part and use it as rich_text
            # Then add the embedded files as children
            text_only = re.sub(r'!\[\[[^\]]+\]\]', '', text).strip()
            rich_text = self._parse_rich_text(text_only) if text_only else []
            
            # Create children for embedded files
            children = []
            embeds = re.findall(r'!\[\[([^|\]]+)(\|([^\]]+))?\]\]', text)
            
            for embed in embeds:
                filename = embed[0].strip()
                display_name = embed[2] if embed[2] else None
                
                # Check if filename is in asset_mapping directly
                if filename in asset_mapping:
                    upload_id = asset_mapping[filename]
                    file_block = self._create_file_block(filename, upload_id, display_name)
                    children.append(file_block)
                else:
                    # Create placeholder for missing files
                    missing_block = self._create_missing_file_block(filename)
                    children.append(missing_block)
            
            result = {"rich_text": rich_text}
            if children:
                result["children"] = children
            return result
        else:
            # No embeds, just rich text
            return {"rich_text": self._parse_rich_text(text)}
    
    def _scan_vault(self) -> List[MarkdownFile]:
        """Scan vault directory for Markdown files and extract metadata"""
        vault_path = Path(self.config.source_vault_path)
        markdown_files = []
        
        # Determine scan path - either subfolder or entire vault
        if self.config.target_subfolder:
            scan_path = vault_path / self.config.target_subfolder
            self.logger.info(f"Scanning subfolder: {scan_path}")
        else:
            scan_path = vault_path
            self.logger.info(f"Scanning vault at: {vault_path}")
        
        if not scan_path.exists():
            raise ValueError(f"Scan path does not exist: {scan_path}")
        
        # Find all .md files recursively, case-insensitively
        self.logger.info("Starting recursive file scan...")
        all_files = list(scan_path.rglob("*"))
        self.logger.info(f"Total items found in scan path: {len(all_files)}")

        for item_path in all_files:
            if item_path.is_file() and item_path.suffix.lower() == '.md':
                try:
                    markdown_data = self._parse_markdown_file(item_path)
                    markdown_files.append(markdown_data)
                except Exception as e:
                    self.logger.error(f"Error parsing {item_path}: {e}")
        
        self.logger.info(f"Found {len(markdown_files)} Markdown files")
        return markdown_files
    
    def _parse_markdown_file(self, file_path: Path) -> MarkdownFile:
        """Parse a markdown file and extract frontmatter and content"""
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        frontmatter = {}
        main_content = content
        
        # Extract YAML frontmatter if present
        if content.startswith('---'):
            try:
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    frontmatter_text = parts[1].strip()
                    main_content = parts[2].strip()
                    
                    if frontmatter_text:
                        frontmatter = yaml.safe_load(frontmatter_text) or {}
            except Exception as e:
                self.logger.warning(f"Error parsing frontmatter in {file_path}: {e}")
        
        # Extract file references from content
        file_references = self._extract_file_references(main_content)
        
        # Determine title (from frontmatter, heading, or filename)
        title = self._extract_title(frontmatter, main_content, file_path)
        
        return MarkdownFile(
            path=file_path,
            title=title,
            content=main_content,
            frontmatter=frontmatter,
            file_references=file_references
        )
    
    def _extract_title(self, frontmatter: Dict, content: str, file_path: Path) -> str:
        """Extract title from frontmatter or filename (like Obsidian)"""
        # Priority: frontmatter title > filename (like Obsidian behavior)
        if 'title' in frontmatter:
            return str(frontmatter['title'])
        
        # Use filename as title (Obsidian behavior)
        return file_path.stem
    
    def _extract_file_references(self, content: str) -> List[Tuple[str, Optional[str]]]:
        """Extract all file references from Markdown content"""
        references = []
        
        # Obsidian embed syntax: ![[filename]] or ![[filename|display_name]]
        embed_pattern = r'!\[\[([^|\]]+)(\|([^\]]+))?\]\]'
        for match in re.finditer(embed_pattern, content):
            filename = match.group(1).strip()
            display_name = match.group(3) if match.group(3) else None
            references.append((filename, display_name))
        
        # Standard Markdown image syntax: ![alt](path)
        markdown_image_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
        for match in re.finditer(markdown_image_pattern, content):
            alt_text = match.group(1)
            file_path = match.group(2)
            # Extract filename from path
            filename = Path(file_path).name
            references.append((filename, alt_text if alt_text else None))
        
        # Standard Markdown link syntax for files: [text](file.ext)
        file_link_pattern = r'\[([^\]]+)\]\(([^)]+\.(pdf|doc|docx|zip|mp4|mov|mp3|wav))\)'
        for match in re.finditer(file_link_pattern, content, re.IGNORECASE):
            link_text = match.group(1)
            file_path = match.group(2)
            filename = Path(file_path).name
            references.append((filename, link_text))
        
        return references
    
    def _resolve_file_path(self, filename: str, markdown_file_path: Path) -> Optional[Path]:
        """Resolve file path relative to markdown file or vault root"""
        vault_root = Path(self.config.source_vault_path)
        
        # Try both original filename and URL-decoded version
        filenames_to_try = [filename]
        if '%' in filename:
            decoded_filename = unquote(filename)
            if decoded_filename != filename:
                filenames_to_try.append(decoded_filename)
        
        # Common search locations
        search_paths = []
        for fname in filenames_to_try:
            search_paths.extend([
                # Same directory as markdown file
                markdown_file_path.parent / fname,
                # Attachments folder relative to markdown file
                markdown_file_path.parent / self.config.attachments_folder / fname,
                # Vault root
                vault_root / fname,
                # Attachments folder in vault root
                vault_root / self.config.attachments_folder / fname,
                # Common asset folders
                vault_root / "assets" / fname,
                vault_root / "files" / fname,
                vault_root / "media" / fname,
            ])
        
        # Search for file with various extensions if no extension provided
        if not Path(filename).suffix:
            extensions = ['.png', '.jpg', '.jpeg', '.gif', '.pdf', '.mp4', '.mov']
            expanded_paths = []
            for path in search_paths:
                for ext in extensions:
                    expanded_paths.append(path.with_suffix(ext))
            search_paths.extend(expanded_paths)
        
        # Find the first existing file
        for path in search_paths:
            if path.exists() and path.is_file():
                return path
        
        # Recursive search as fallback
        for fname in filenames_to_try:
            for path in vault_root.rglob(fname):
                if path.is_file():
                    return path
        
        return None
    
    def _analyze_file(self, file_path: Path) -> FileInfo:
        """Analyze a file and return its information"""
        # Get file size
        size = file_path.stat().st_size
        
        # Get MIME type
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if not mime_type:
            mime_type = 'application/octet-stream'
        
        # Calculate file hash for deduplication
        file_hash = self._calculate_file_hash(file_path)
        
        return FileInfo(
            path=file_path,
            name=file_path.name,
            size=size,
            mime_type=mime_type,
            hash=file_hash
        )
    
    def _calculate_file_hash(self, file_path: Path) -> str:
        """Calculate SHA-256 hash of file for deduplication"""
        hasher = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    
    def _validate_file_for_upload(self, file_info: FileInfo) -> Tuple[bool, Optional[str]]:
        """Validate if file can be uploaded to Notion"""
        # Check file size against Notion's 100MB limit
        max_size = 100 * 1024 * 1024  # 100MB
        if file_info.size > max_size:
            size_mb = file_info.size / (1024 * 1024)
            return False, f"File too large: {size_mb:.1f}MB (max: 100MB)"
        
        # Check file extension
        file_ext = file_info.path.suffix.lower()
        if file_ext not in self.config.supported_extensions:
            return False, f"Unsupported file type: {file_ext}"
        
        # Check if file exists
        if not file_info.path.exists():
            return False, f"File not found: {file_info.path}"
        
        return True, None
    
    def _upload_file_to_notion(self, file_info: FileInfo) -> UploadResult:
        """Upload a file to Notion using standard or multipart upload based on file size"""
        try:
            # Check if already uploaded (deduplication)
            if file_info.hash in self.uploaded_files:
                self.logger.debug(f"File already uploaded: {file_info.name}")
                return UploadResult(
                    success=True,
                    upload_id=self.uploaded_files[file_info.hash],
                    file_path=str(file_info.path)
                )
            
            # Validate file before upload
            is_valid, error_msg = self._validate_file_for_upload(file_info)
            if not is_valid:
                return UploadResult(
                    success=False,
                    error_message=error_msg,
                    file_path=str(file_info.path)
                )
            
            self.logger.info(f"Uploading file: {file_info.name} ({file_info.size} bytes)")
            
            # Use multipart upload for files >20MB
            if file_info.size > 20 * 1024 * 1024:
                return self._upload_file_multipart(file_info)
            else:
                return self._upload_file_standard(file_info)
            
        except Exception as e:
            error_msg = f"Failed to upload {file_info.name}: {str(e)}"
            self.logger.error(error_msg)
            self.failed_files.append(str(file_info.path))
            
            return UploadResult(
                success=False,
                error_message=error_msg,
                file_path=str(file_info.path)
            )
    
    def _upload_file_standard(self, file_info: FileInfo) -> UploadResult:
        """Upload a file ≤20MB using standard single-request upload"""
        sanitized_filename = self._sanitize_filename(file_info.name)
        self.logger.debug(f"Sanitized filename: {file_info.name} -> {sanitized_filename}")
        
        # Step 1: Create file upload object
        create_response = self.session.post(
            "https://api.notion.com/v1/file_uploads",
            headers={
                "Authorization": f"Bearer {self.config.notion_token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json"
            },
            json={
                "filename": sanitized_filename,
                "file_size": file_info.size
            },
            timeout=30
        )
        create_response.raise_for_status()
        upload_data = create_response.json()
        
        file_upload_id = upload_data["id"]
        
        # Step 2: Send file content
        with open(file_info.path, 'rb') as f:
            send_response = self.session.post(
                f"https://api.notion.com/v1/file_uploads/{file_upload_id}/send",
                headers={
                    "Authorization": f"Bearer {self.config.notion_token}",
                    "Notion-Version": "2022-06-28"
                },
                files={
                    'file': (sanitized_filename, f, file_info.mime_type)
                },
                timeout=60
            )
            send_response.raise_for_status()
        
        # Cache successful upload
        self.uploaded_files[file_info.hash] = file_upload_id
        self.logger.info(f"Successfully uploaded: {file_info.name} -> {file_upload_id}")
        
        # Rate limiting
        time.sleep(DEFAULT_CONFIG['rate_limit_delay'])
        
        return UploadResult(
            success=True,
            upload_id=file_upload_id,
            file_path=str(file_info.path)
        )
    
    def _upload_file_multipart(self, file_info: FileInfo) -> UploadResult:
        """Upload a file >20MB using multipart upload with part_number"""
        sanitized_filename = self._sanitize_filename(file_info.name)
        self.logger.info(f"Using multipart upload for large file: {file_info.name}")
        
        # Calculate number of parts needed
        chunk_size = 20 * 1024 * 1024  # 20MB chunks
        number_of_parts = (file_info.size + chunk_size - 1) // chunk_size  # Ceiling division
        self.logger.debug(f"File will be split into {number_of_parts} parts")
        
        # Step 1: Create file upload object with multipart mode
        try:
            create_response = self.session.post(
                "https://api.notion.com/v1/file_uploads",
                headers={
                    "Authorization": f"Bearer {self.config.notion_token}",
                    "Notion-Version": "2022-06-28",
                    "Content-Type": "application/json"
                },
                json={
                    "filename": sanitized_filename,
                    "file_size": file_info.size,
                    "mode": "multi_part",
                    "number_of_parts": number_of_parts
                },
                timeout=30
            )
            self.logger.debug(f"Create upload response: {create_response.status_code}, {create_response.text}")
            create_response.raise_for_status()
        except Exception as e:
            self.logger.error(f"Failed to create multipart upload: {e}")
            if hasattr(create_response, 'text'):
                self.logger.error(f"Response body: {create_response.text}")
            raise
        upload_data = create_response.json()
        
        file_upload_id = upload_data["id"]
        
        # Step 2: Upload file in parts (max 20MB per part)
        part_number = 1
        
        with open(file_info.path, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                
                self.logger.debug(f"Uploading part {part_number} for {file_info.name}")
                
                # Send each part with part_number
                try:
                    part_response = self.session.post(
                        f"https://api.notion.com/v1/file_uploads/{file_upload_id}/send",
                        headers={
                            "Authorization": f"Bearer {self.config.notion_token}",
                            "Notion-Version": "2022-06-28"
                        },
                        files={
                            'file': (sanitized_filename, chunk, file_info.mime_type)
                        },
                        data={
                            'part_number': str(part_number)
                        },
                        timeout=120
                    )
                    self.logger.debug(f"Part {part_number} response: {part_response.status_code}, {part_response.text}")
                    part_response.raise_for_status()
                except Exception as e:
                    self.logger.error(f"Failed to upload part {part_number}: {e}")
                    if hasattr(part_response, 'text'):
                        self.logger.error(f"Part {part_number} response body: {part_response.text}")
                    raise
                
                part_number += 1
                
                # Rate limiting between parts
                time.sleep(DEFAULT_CONFIG['rate_limit_delay'])
        
        # Step 3: Complete the multipart upload
        self.logger.debug(f"Completing multipart upload for {file_info.name}")
        try:
            complete_response = self.session.post(
                f"https://api.notion.com/v1/file_uploads/{file_upload_id}/complete",
                headers={
                    "Authorization": f"Bearer {self.config.notion_token}",
                    "Notion-Version": "2022-06-28",
                    "Content-Type": "application/json"
                },
                json={},
                timeout=30
            )
            self.logger.debug(f"Complete response: {complete_response.status_code}, {complete_response.text}")
            complete_response.raise_for_status()
        except Exception as e:
            self.logger.error(f"Failed to complete multipart upload: {e}")
            if hasattr(complete_response, 'text'):
                self.logger.error(f"Complete response body: {complete_response.text}")
            raise
        
        # Cache successful upload
        self.uploaded_files[file_info.hash] = file_upload_id
        self.logger.info(f"Successfully uploaded (multipart): {file_info.name} -> {file_upload_id}")
        
        return UploadResult(
            success=True,
            upload_id=file_upload_id,
            file_path=str(file_info.path)
        )
    
    def _batch_upload_files(self, file_infos: List[FileInfo]) -> Dict[str, str]:
        """Upload multiple files concurrently with progress tracking"""
        upload_mapping = {}  # filename -> upload_id
        
        if not file_infos:
            return upload_mapping
        
        self.logger.info(f"Starting batch upload of {len(file_infos)} files")
        
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            # Submit upload tasks
            future_to_file = {
                executor.submit(self._upload_file_to_notion, file_info): file_info
                for file_info in file_infos
            }
            
            # Process results with progress bar
            with tqdm(total=len(file_infos), desc="Uploading files", unit="file") as pbar:
                for future in as_completed(future_to_file):
                    file_info = future_to_file[future]
                    try:
                        result = future.result()
                        if result.success:
                            # Add filename key for standard lookups
                            upload_mapping[file_info.name] = result.upload_id
                            
                            # Also add relative path key for full path lookups from markdown
                            vault_root = Path(self.config.source_vault_path)
                            try:
                                relative_path = file_info.path.relative_to(vault_root)
                                upload_mapping[str(relative_path)] = result.upload_id
                            except ValueError:
                                pass  # Skip if not relative to vault root
                        else:
                            self.logger.error(f"Upload failed: {result.error_message}")
                    except Exception as e:
                        self.logger.error(f"Exception during upload of {file_info.name}: {e}")
                    finally:
                        pbar.update(1)
        
        successful_uploads = len([r for r in upload_mapping.values() if r])
        self.logger.info(f"Batch upload completed: {successful_uploads}/{len(file_infos)} successful")
        
        return upload_mapping
    
    def _markdown_to_notion_blocks(self, content: str, asset_mapping: Dict[str, str]) -> List[Dict]:
        """Convert Markdown content to Notion blocks"""
        blocks = []
        lines = content.split('\n')
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Skip empty lines
            if not line:
                i += 1
                continue
            
            # Handle code blocks
            if line.startswith('```'):
                code_block, lines_consumed = self._parse_code_block(lines[i:])
                blocks.append(code_block)
                i += lines_consumed
                continue
            
            # Handle headings
            if line.startswith('#'):
                blocks.append(self._create_heading_block(line))
                i += 1
                continue
            
            # Handle lists
            if line.startswith('- ') or line.startswith('* ') or re.match(r'^\d+\.', line):
                list_blocks, lines_consumed = self._parse_list(lines[i:], asset_mapping)
                blocks.extend(list_blocks)
                i += lines_consumed
                continue
            
            # Handle blockquotes
            if line.startswith('>'):
                blocks.append(self._create_quote_block(line))
                i += 1
                continue
            
            # Handle file embeds
            if '![[' in line:
                embed_blocks = self._process_embeds_in_line(line, asset_mapping)
                blocks.extend(embed_blocks)
                i += 1
                continue
            
            # Handle regular paragraphs
            paragraph_block = self._create_paragraph_block(line)
            blocks.append(paragraph_block)
            i += 1
        
        return blocks
    
    def _parse_code_block(self, lines: List[str]) -> Tuple[Dict, int]:
        """Parse a code block and return the block and number of lines consumed"""
        language = lines[0][3:].strip() if len(lines[0]) > 3 else ""
        code_lines = []
        lines_consumed = 1
        
        for i in range(1, len(lines)):
            if lines[i].strip().startswith('```'):
                lines_consumed = i + 1
                break
            code_lines.append(lines[i])
        
        code_content = '\n'.join(code_lines)
        
        return {
            "type": "code",
            "code": {
                "caption": [],
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": code_content}
                    }
                ],
                "language": self._normalize_code_language(language)
            }
        }, lines_consumed
    
    def _parse_list(self, lines: List[str], asset_mapping: Dict[str, str]) -> Tuple[List[Dict], int]:
        """Parse a list and return list item blocks with proper nesting and number of lines consumed"""
        list_blocks = []
        lines_consumed = 0
        i = 0
        
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                break
            
            # Calculate indentation level
            indent_level = len(line) - len(line.lstrip())
            line_content = line.strip()
            
            # Check if it's a list item
            if line_content.startswith('- ') or line_content.startswith('* '):
                list_item_text = line_content[2:].strip()
                
                # Create the list item block
                list_item = {
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        **self._parse_list_item_content(list_item_text, asset_mapping)
                    }
                }
                
                # Look ahead for nested items
                children, child_lines = self._parse_nested_list_items(lines[i+1:], indent_level, asset_mapping)
                if children:
                    list_item["bulleted_list_item"]["children"] = children
                
                list_blocks.append(list_item)
                lines_consumed = i + 1 + child_lines
                i += 1 + child_lines
                
            elif re.match(r'^\d+\.', line_content):
                # Numbered list
                list_item_text = re.sub(r'^\d+\.\s*', '', line_content)
                
                list_item = {
                    "type": "numbered_list_item",
                    "numbered_list_item": {
                        **self._parse_list_item_content(list_item_text, asset_mapping)
                    }
                }
                
                # Look ahead for nested items
                children, child_lines = self._parse_nested_list_items(lines[i+1:], indent_level, asset_mapping)
                if children:
                    list_item["numbered_list_item"]["children"] = children
                
                list_blocks.append(list_item)
                lines_consumed = i + 1 + child_lines
                i += 1 + child_lines
            else:
                break
        
        return list_blocks, lines_consumed
    
    def _parse_nested_list_items(self, lines: List[str], parent_indent: int, asset_mapping: Dict[str, str]) -> Tuple[List[Dict], int]:
        """Parse nested list items that are indented more than the parent"""
        nested_blocks = []
        lines_consumed = 0
        i = 0
        
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                lines_consumed += 1
                continue
            
            # Calculate indentation level
            indent_level = len(line) - len(line.lstrip())
            line_content = line.strip()
            
            # If indentation is less than or equal to parent, we're done with nested items
            if indent_level <= parent_indent:
                break
            
            # Check if it's a list item with more indentation than parent
            if line_content.startswith('- ') or line_content.startswith('* '):
                list_item_text = line_content[2:].strip()
                
                list_item = {
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        **self._parse_list_item_content(list_item_text, asset_mapping)
                    }
                }
                
                # Look for further nested items
                children, child_lines = self._parse_nested_list_items(lines[i+1:], indent_level, asset_mapping)
                if children:
                    list_item["bulleted_list_item"]["children"] = children
                
                nested_blocks.append(list_item)
                lines_consumed = i + 1 + child_lines
                i += 1 + child_lines
                
            elif re.match(r'^\d+\.', line_content):
                # Numbered nested list
                list_item_text = re.sub(r'^\d+\.\s*', '', line_content)
                
                list_item = {
                    "type": "numbered_list_item",
                    "numbered_list_item": {
                        **self._parse_list_item_content(list_item_text, asset_mapping)
                    }
                }
                
                # Look for further nested items
                children, child_lines = self._parse_nested_list_items(lines[i+1:], indent_level, asset_mapping)
                if children:
                    list_item["numbered_list_item"]["children"] = children
                
                nested_blocks.append(list_item)
                lines_consumed = i + 1 + child_lines
                i += 1 + child_lines
            else:
                # Not a list item, stop parsing nested items
                break
        
        return nested_blocks, lines_consumed
    
    def _process_embeds_in_line(self, line: str, asset_mapping: Dict[str, str]) -> List[Dict]:
        """Process embedded files in a line and create appropriate blocks"""
        blocks = []
        
        # Split line by embeds
        parts = re.split(r'(!\[\[[^\]]+\]\])', line)
        
        for part in parts:
            if part.startswith('![[') and part.endswith(']]'):
                # Handle embed
                embed_match = re.match(r'!\[\[([^|\]]+)(\|([^\]]+))?\]\]', part)
                if embed_match:
                    filename = embed_match.group(1).strip()
                    display_name = embed_match.group(3)
                    
                    if filename in asset_mapping:
                        file_block = self._create_file_block(
                            filename, 
                            asset_mapping[filename], 
                            display_name
                        )
                        blocks.append(file_block)
                    else:
                        # Create placeholder for missing files
                        placeholder_block = self._create_missing_file_block(filename)
                        blocks.append(placeholder_block)
            else:
                # Handle text content
                if part.strip():
                    text_block = self._create_paragraph_block(part.strip())
                    blocks.append(text_block)
        
        return blocks
    
    def _create_file_block(self, filename: str, upload_id: str, display_name: Optional[str] = None) -> Dict:
        """Create appropriate Notion block for uploaded file"""
        file_ext = Path(filename).suffix.lower()
        
        # Determine block type based on file extension
        if file_ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
            block_type = 'image'
        elif file_ext == '.pdf':
            block_type = 'pdf'
        elif file_ext in ['.mp4', '.mov', '.avi', '.mkv']:
            block_type = 'video'
        elif file_ext in ['.mp3', '.wav', '.ogg', '.m4a']:
            block_type = 'audio'
        else:
            block_type = 'file'
        
        block = {
            "type": block_type,
            block_type: {
                "type": "file_upload",
                "file_upload": {
                    "id": upload_id
                }
            }
        }
        
        # Add caption if display name is provided
        if display_name and block_type in ['image', 'file', 'pdf']:
            block[block_type]['caption'] = [
                {
                    "type": "text",
                    "text": {"content": display_name}
                }
            ]
        
        return block
    
    def _create_missing_file_block(self, filename: str) -> Dict:
        """Create a callout block for missing files"""
        return {
            "type": "callout",
            "callout": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"⚠️ Missing file: {filename}"}
                    }
                ],
                "icon": {"emoji": "⚠️"},
                "color": "yellow"
            }
        }
    
    def _create_paragraph_block(self, text: str) -> Dict:
        """Create a paragraph block with rich text formatting"""
        rich_text = self._parse_rich_text(text)
        return {
            "type": "paragraph",
            "paragraph": {
                "rich_text": rich_text
            }
        }
    
    def _parse_rich_text(self, text: str) -> List[Dict]:
        """Parse text and convert formatting to Notion rich text"""
        if not text:
            return []
        
        # Find all formatting patterns and their positions
        patterns = []
        
        # Find bold patterns **text**
        for match in re.finditer(r'\*\*(.*?)\*\*', text):
            patterns.append((match.start(), match.end(), 'bold', match.group(1)))
        
        # Find italic patterns *text*
        for match in re.finditer(r'\*([^*]+?)\*', text):
            # Make sure it's not part of a bold pattern
            if not any(match.start() >= p[0] and match.end() <= p[1] for p in patterns):
                patterns.append((match.start(), match.end(), 'italic', match.group(1)))
        
        # Find code patterns `text`
        for match in re.finditer(r'`([^`]+?)`', text):
            if not any(match.start() >= p[0] and match.end() <= p[1] for p in patterns):
                patterns.append((match.start(), match.end(), 'code', match.group(1)))
        
        # Find link patterns [text](url)
        for match in re.finditer(r'\[([^\]]+)\]\(([^)]+)\)', text):
            if not any(match.start() >= p[0] and match.end() <= p[1] for p in patterns):
                patterns.append((match.start(), match.end(), 'link', match.group(1), match.group(2)))
        
        # Sort patterns by start position
        patterns.sort(key=lambda x: x[0])
        
        # Build rich text array
        rich_text = []
        current_pos = 0
        
        for pattern in patterns:
            start, end, pattern_type = pattern[0], pattern[1], pattern[2]
            
            # Add plain text before this pattern
            if start > current_pos:
                plain_text = text[current_pos:start]
                if plain_text:
                    rich_text.append({
                        "type": "text",
                        "text": {"content": plain_text}
                    })
            
            # Add formatted text
            if pattern_type == 'bold':
                rich_text.append({
                    "type": "text",
                    "text": {"content": pattern[3]},
                    "annotations": {"bold": True}
                })
            elif pattern_type == 'italic':
                rich_text.append({
                    "type": "text",
                    "text": {"content": pattern[3]},
                    "annotations": {"italic": True}
                })
            elif pattern_type == 'code':
                rich_text.append({
                    "type": "text",
                    "text": {"content": pattern[3]},
                    "annotations": {"code": True}
                })
            elif pattern_type == 'link':
                rich_text.append({
                    "type": "text",
                    "text": {"content": pattern[3], "link": {"url": pattern[4]}}
                })
            
            current_pos = end
        
        # Add any remaining plain text
        if current_pos < len(text):
            remaining_text = text[current_pos:]
            if remaining_text:
                rich_text.append({
                    "type": "text",
                    "text": {"content": remaining_text}
                })
        
        return rich_text if rich_text else [{"type": "text", "text": {"content": text}}]
    
    def _create_heading_block(self, line: str) -> Dict:
        """Create heading block based on markdown heading level"""
        if line.startswith('### '):
            heading_type = 'heading_3'
            text = line[4:].strip()
        elif line.startswith('## '):
            heading_type = 'heading_2'
            text = line[3:].strip()
        else:  # Default to h1
            heading_type = 'heading_1'
            text = line.lstrip('#').strip()
        
        return {
            "type": heading_type,
            heading_type: {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": text}
                    }
                ]
            }
        }
    
    def _create_quote_block(self, line: str) -> Dict:
        """Create a quote block"""
        quote_text = line[1:].strip()
        return {
            "type": "quote",
            "quote": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": quote_text}
                    }
                ]
            }
        }
    
    def _create_database_entry(self, markdown_file: MarkdownFile, blocks: List[Dict]) -> Optional[str]:
        """Create a new database entry in Notion with the given blocks"""
        try:
            # Prepare database properties
            properties = self._prepare_database_properties(markdown_file)
            
            # Chunk blocks to avoid API limits (max 100 blocks per request)
            block_chunks = [blocks[i:i+100] for i in range(0, len(blocks), 100)]
            
            # Create the database entry with first chunk of blocks
            first_chunk = block_chunks[0] if block_chunks else []
            
            entry_data = {
                "parent": {"database_id": self.config.target_database_id},
                "properties": properties,
                "children": first_chunk
            }
            
            # Create the database entry
            page_response = self.notion.pages.create(**entry_data)
            page_id = page_response["id"]
            
            self.logger.info(f"Created database entry: {markdown_file.title} -> {page_id}")
            
            # Add remaining block chunks
            for chunk in block_chunks[1:]:
                self._append_blocks_to_page(page_id, chunk)
                time.sleep(DEFAULT_CONFIG['rate_limit_delay'])
            
            return page_id
            
        except Exception as e:
            self.logger.error(f"Failed to create database entry '{markdown_file.title}': {str(e)}")
            return None
    
    def _prepare_database_properties(self, markdown_file: MarkdownFile) -> Dict[str, any]:
        """Prepare database properties from markdown file metadata"""
        properties = {}
        
        # Title property (required for databases)
        properties["Name"] = {
            "title": [
                {
                    "type": "text",
                    "text": {"content": markdown_file.title}
                }
            ]
        }
        
        # Source file property (only add if it exists in database)
        # Skip for now since the database may not have this property
        
        # Extract tags from frontmatter
        if 'tags' in markdown_file.frontmatter:
            tags = markdown_file.frontmatter['tags']
            if isinstance(tags, str):
                tags = [tag.strip() for tag in tags.split(',')]
            elif isinstance(tags, list):
                tags = [str(tag) for tag in tags]
            
            if tags:
                properties["Tags"] = {
                    "multi_select": [{"name": tag} for tag in tags[:100]]  # Notion limit
                }
        
        # Handle custom frontmatter properties
        for key, value in markdown_file.frontmatter.items():
            if key in ['title', 'tags']:  # Skip already handled
                continue
                
            # Convert various frontmatter types to Notion properties
            if isinstance(value, str) and len(value) <= 2000:  # Rich text limit
                properties[key.title()] = {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": value}
                        }
                    ]
                }
            elif isinstance(value, (int, float)):
                properties[key.title()] = {"number": value}
            elif isinstance(value, bool):
                properties[key.title()] = {"checkbox": value}
            elif isinstance(value, list):
                # Convert to multi-select if strings
                if all(isinstance(item, str) for item in value):
                    properties[key.title()] = {
                        "multi_select": [{"name": str(item)} for item in value[:100]]
                    }
        
        return properties
    
    def _append_blocks_to_page(self, page_id: str, blocks: List[Dict]) -> bool:
        """Append blocks to an existing page"""
        try:
            self.notion.blocks.children.append(
                block_id=page_id,
                children=blocks
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to append blocks to page {page_id}: {str(e)}")
            return False
    
    def _migrate_single_file(self, markdown_file: MarkdownFile, asset_mapping: Dict[str, str]) -> Optional[str]:
        """Migrate a single Markdown file to Notion database"""
        try:
            # Convert to Notion blocks
            blocks = self._markdown_to_notion_blocks(markdown_file.content, asset_mapping)
            
            if self.config.dry_run:
                self.logger.info(f"[DRY RUN] Would create database entry: {markdown_file.title} with {len(blocks)} blocks")
                return "dry-run-page-id"
            
            # Create database entry in Notion
            page_id = self._create_database_entry(markdown_file, blocks)
            
            if page_id:
                self.logger.info(f"Successfully migrated: {markdown_file.path.name} -> {page_id}")
                return page_id
            else:
                self.failed_files.append(str(markdown_file.path))
                return None
                
        except Exception as e:
            self.logger.error(f"Failed to migrate {markdown_file.path}: {str(e)}")
            self.failed_files.append(str(markdown_file.path))
            return None
    
    def migrate_vault(self) -> Dict[str, any]:
        """Main migration method - orchestrates the entire process"""
        migration_start_time = time.time()
        
        try:
            self.logger.info("Starting Obsidian to Notion database migration")
            
            # Phase 1: Discover files
            self.logger.info("Phase 1: Discovering files...")
            markdown_files = self._scan_vault()
            
            if not markdown_files:
                self.logger.warning("No Markdown files found in vault")
                return self._create_migration_report([], {}, [], migration_start_time)
            
            # Phase 2: Extract and analyze assets
            self.logger.info("Phase 2: Analyzing assets...")
            all_assets = self._discover_all_assets(markdown_files)
            
            self.logger.info(f"Found {len(all_assets)} unique assets to upload")
            
            # Phase 3: Upload assets
            if not self.config.dry_run:
                self.logger.info("Phase 3: Uploading assets...")
                asset_mapping = self._batch_upload_files(all_assets)
            else:
                # Create dummy mapping for dry run with both filename and path keys
                asset_mapping = {}
                vault_root = Path(self.config.source_vault_path)
                for i, asset in enumerate(all_assets):
                    upload_id = f"dry-run-upload-{i}"
                    asset_mapping[asset.name] = upload_id  # filename key
                    try:
                        relative_path = asset.path.relative_to(vault_root)
                        asset_mapping[str(relative_path)] = upload_id  # path key
                    except ValueError:
                        pass  # Skip if not relative to vault root
            
            # Phase 4: Migrate pages to database
            self.logger.info("Phase 4: Creating database entries...")
            migrated_pages = []
            
            with tqdm(total=len(markdown_files), desc="Creating database entries", unit="entry") as pbar:
                for md_file in markdown_files:
                    page_id = self._migrate_single_file(md_file, asset_mapping)
                    if page_id:
                        migrated_pages.append({
                            'source_file': str(md_file.path),
                            'page_id': page_id,
                            'title': md_file.title,
                            'frontmatter': md_file.frontmatter
                        })
                    
                    pbar.update(1)
                    time.sleep(DEFAULT_CONFIG['rate_limit_delay'])
            
            # Generate final report
            return self._create_migration_report(
                migrated_pages, 
                asset_mapping, 
                all_assets, 
                migration_start_time
            )
            
        except Exception as e:
            self.logger.error(f"Migration failed: {str(e)}")
            raise
    
    def _discover_all_assets(self, markdown_files: List[MarkdownFile]) -> List[FileInfo]:
        """Discover all unique assets referenced in markdown files"""
        all_file_refs = set()
        
        # Extract file references from all markdown files
        for md_file in markdown_files:
            try:
                for filename, _ in md_file.file_references:
                    file_path = self._resolve_file_path(filename, md_file.path)
                    if file_path:
                        all_file_refs.add(file_path)
                    else:
                        self.logger.warning(f"Could not resolve file: {filename} (referenced in {md_file.path.name})")
                        
            except Exception as e:
                self.logger.error(f"Error processing {md_file.path}: {str(e)}")
        
        # Analyze discovered files
        file_infos = []
        for file_path in all_file_refs:
            try:
                file_info = self._analyze_file(file_path)
                file_infos.append(file_info)
            except Exception as e:
                self.logger.error(f"Error analyzing file {file_path}: {str(e)}")
        
        return file_infos
    
    def _create_migration_report(self, migrated_pages: List[Dict], asset_mapping: Dict[str, str], 
                               all_assets: List[FileInfo], start_time: float) -> Dict[str, any]:
        """Create comprehensive migration report"""
        end_time = time.time()
        duration = end_time - start_time
        
        total_assets = len(all_assets)
        # Count unique upload IDs since asset_mapping has duplicate entries (filename + path)
        unique_upload_ids = set(upload_id for upload_id in asset_mapping.values() if upload_id)
        successful_uploads = len(unique_upload_ids)
        failed_uploads = total_assets - successful_uploads
        
        successful_pages = len(migrated_pages)
        failed_pages = len(self.failed_files)
        
        report = {
            "migration_summary": {
                "duration_seconds": round(duration, 2),
                "total_pages_processed": successful_pages + failed_pages,
                "successful_pages": successful_pages,
                "failed_pages": failed_pages,
                "total_assets": total_assets,
                "successful_uploads": successful_uploads,
                "failed_uploads": failed_uploads,
                "dry_run": self.config.dry_run
            },
            "migrated_pages": migrated_pages,
            "uploaded_assets": {
                filename: upload_id 
                for filename, upload_id in asset_mapping.items()
            },
            "failed_files": self.failed_files,
            "asset_stats": {
                "total_size_bytes": sum(asset.size for asset in all_assets),
                "average_size_bytes": sum(asset.size for asset in all_assets) // len(all_assets) if all_assets else 0,
                "file_types": self._get_file_type_distribution(all_assets)
            }
        }
        
        # Log summary
        self.logger.info(f"Migration completed in {duration:.2f} seconds")
        self.logger.info(f"Pages: {successful_pages} successful, {failed_pages} failed")
        self.logger.info(f"Assets: {successful_uploads} uploaded, {failed_uploads} failed")
        
        return report
    
    def _get_file_type_distribution(self, assets: List[FileInfo]) -> Dict[str, int]:
        """Get distribution of file types in assets"""
        type_counts = {}
        for asset in assets:
            ext = asset.path.suffix.lower()
            type_counts[ext] = type_counts.get(ext, 0) + 1
        return type_counts


import argparse
import sys

def load_config_from_file(config_path: str) -> Dict:
    """Load configuration from YAML file"""
    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logging.error(f"Failed to load config file {config_path}: {e}")
        return {}

def create_sample_config_file(path: str):
    """Create a sample configuration file"""
    sample_config = {
        'notion_token': 'YOUR_NOTION_TOKEN_HERE',
        'target_database_id': 'YOUR_TARGET_DATABASE_ID_HERE', 
        'source_vault_path': '/path/to/your/obsidian/vault',
        'attachments_folder': 'attachments',
        'batch_size': 10,
        'max_workers': 3,
        'max_file_size': 20971520,  # 20MB
        'dry_run': False,
        'extract_frontmatter': True,
        'database_properties': {
            'Name': {'type': 'title'},
            'Tags': {'type': 'multi_select'},
            'Created': {'type': 'created_time'},
            'Modified': {'type': 'last_edited_time'},
            'Source File': {'type': 'rich_text'}
        }
    }
    
    with open(path, 'w') as f:
        yaml.dump(sample_config, f, default_flow_style=False)
    
    print(f"Sample configuration file created at: {path}")
    print("Please edit the file with your actual values before running the migration.")
    print("\nTo get your database ID:")
    print("1. Open your Notion database")
    print("2. Copy the URL - the database ID is the long string before any '?' parameters")
    print("3. Example: https://notion.so/myworkspace/DATABASE_ID_HERE?v=...")

def main():
    parser = argparse.ArgumentParser(
        description="Migrate Obsidian vault to Notion database with file uploads",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create sample config file
  python obsidian_migrator.py --create-config config.yaml
  
  # Run migration with config file
  python obsidian_migrator.py --config config.yaml
  
  # Run with command line arguments
  python obsidian_migrator.py --vault /path/to/vault --target-database DATABASE_ID --token TOKEN
  
  # Dry run to preview migration
  python obsidian_migrator.py --config config.yaml --dry-run
        """
    )
    
    # Configuration options
    parser.add_argument('--config', '-c', help='Configuration file path (YAML)')
    parser.add_argument('--create-config', help='Create sample configuration file at specified path')
    
    # Direct configuration options
    parser.add_argument('--vault', help='Obsidian vault directory path')
    parser.add_argument('--target-database', help='Notion target database ID') 
    parser.add_argument('--token', help='Notion API token (or set NOTION_TOKEN env var)')
    parser.add_argument('--attachments-folder', default='attachments', help='Attachments folder name')
    
    # Migration options
    parser.add_argument('--dry-run', action='store_true', help='Preview migration without uploading')
    parser.add_argument('--max-workers', type=int, default=3, help='Maximum concurrent uploads')
    parser.add_argument('--batch-size', type=int, default=10, help='Batch size for processing')
    parser.add_argument('--no-frontmatter', action='store_true', help='Skip frontmatter extraction')
    
    # Output options
    parser.add_argument('--output', '-o', help='Output file for migration report (JSON)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')
    
    args = parser.parse_args()
    
    # Handle config file creation
    if args.create_config:
        create_sample_config_file(args.create_config)
        return
    
    # Load configuration
    config_data = {}
    if args.config:
        config_data = load_config_from_file(args.config)
    
    # Override with command line arguments
    if args.vault:
        config_data['source_vault_path'] = args.vault
    if args.target_database:
        config_data['target_database_id'] = args.target_database
    if args.token:
        config_data['notion_token'] = args.token
    if args.attachments_folder:
        config_data['attachments_folder'] = args.attachments_folder
    if args.dry_run:
        config_data['dry_run'] = True
    if args.max_workers:
        config_data['max_workers'] = args.max_workers
    if args.batch_size:
        config_data['batch_size'] = args.batch_size
    if args.no_frontmatter:
        config_data['extract_frontmatter'] = False
    
    # Get token from environment if not provided
    if 'notion_token' not in config_data:
        config_data['notion_token'] = os.getenv('NOTION_TOKEN')
    
    # Validate required fields
    required_fields = ['notion_token', 'target_database_id', 'source_vault_path']
    missing_fields = [field for field in required_fields if not config_data.get(field)]
    
    if missing_fields:
        print(f"Error: Missing required configuration: {', '.join(missing_fields)}")
        print("Use --create-config to generate a sample configuration file")
        sys.exit(1)
    
    # Set up logging
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    
    try:
        # Create configuration object
        config = MigrationConfig(**config_data)
        
        # Run migration
        migrator = ObsidianToNotionMigrator(config)
        result = migrator.migrate_vault()
        
        # Output results
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(result, f, indent=2)
            print(f"Migration report saved to: {args.output}")
        else:
            print("\nMigration Results:")
            print(json.dumps(result['migration_summary'], indent=2))
        
        # Exit with error code if there were failures
        if result['migration_summary']['failed_pages'] > 0 or result['migration_summary']['failed_uploads'] > 0:
            sys.exit(1)
            
    except Exception as e:
        print(f"Migration failed: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()