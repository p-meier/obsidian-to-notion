#!/usr/bin/env python3
"""
Test script for Obsidian to Notion migration
"""

import tempfile
import shutil
from pathlib import Path
from obsidian_migrator import ObsidianToNotionMigrator, MigrationConfig

def create_test_vault():
    """Create a test vault with sample files"""
    temp_dir = Path(tempfile.mkdtemp())
    vault_dir = temp_dir / "test_vault"
    vault_dir.mkdir()
    
    # Create sample markdown files with frontmatter
    (vault_dir / "note1.md").write_text("""---
title: Test Note with Metadata
tags: [test, markdown, obsidian]
author: Test User
priority: high
---

# Test Note 1

This is a test note with an embedded image:
![[test_image.png]]

And a link to a PDF:
![[document.pdf|Important Document]]
    """)
    
    (vault_dir / "note2.md").write_text("""---
title: Video Demo Note
tags: [demo, video]
created: 2024-01-15
---

## Another Note

This note has a video:
![[video.mp4]]

And some regular text with **bold** and *italic* formatting.

### Code Example

```python
def hello():
    print("Hello world!")
```

> This is a blockquote

- List item 1
- List item 2
- List item 3
    """)
    
    # Create attachments folder
    attachments_dir = vault_dir / "attachments"
    attachments_dir.mkdir()
    
    # Create dummy files
    (attachments_dir / "test_image.png").write_bytes(b"fake png data")
    (attachments_dir / "document.pdf").write_bytes(b"fake pdf data") 
    (attachments_dir / "video.mp4").write_bytes(b"fake video data")
    
    return vault_dir

def test_dry_run():
    """Test dry run functionality"""
    vault_path = create_test_vault()
    
    try:
        config = MigrationConfig(
            notion_token="fake_token",
            target_database_id="fake_database_id", 
            source_vault_path=str(vault_path),
            dry_run=True
        )
        
        migrator = ObsidianToNotionMigrator(config)
        
        # Test file discovery
        md_files = migrator._scan_vault()
        assert len(md_files) == 2, f"Expected 2 markdown files, found {len(md_files)}"
        
        # Test asset discovery
        assets = migrator._discover_all_assets(md_files)
        assert len(assets) == 3, f"Expected 3 assets, found {len(assets)}"
        
        # Test frontmatter parsing
        note_with_frontmatter = md_files[0] if 'frontmatter' in str(md_files[0].content) else md_files[1]
        assert isinstance(note_with_frontmatter.frontmatter, dict), "Frontmatter should be parsed as dict"
        
        # Test title extraction
        note1 = next(f for f in md_files if f.path.name == "note1.md")
        assert note1.title == "Test Note with Metadata", f"Expected 'Test Note with Metadata', got '{note1.title}'"
        
        # Test file reference extraction
        assert len(note1.file_references) >= 2, f"Expected at least 2 file references, got {len(note1.file_references)}"
        
        print("✅ Dry run test passed")
        
    finally:
        shutil.rmtree(vault_path)

def test_markdown_parsing():
    """Test markdown parsing functionality"""
    vault_path = create_test_vault()
    
    try:
        config = MigrationConfig(
            notion_token="fake_token",
            target_database_id="fake_database_id", 
            source_vault_path=str(vault_path),
            dry_run=True
        )
        
        migrator = ObsidianToNotionMigrator(config)
        md_files = migrator._scan_vault()
        
        # Test block conversion
        note2 = next(f for f in md_files if f.path.name == "note2.md")
        blocks = migrator._markdown_to_notion_blocks(note2.content, {})
        
        # Should have various block types
        block_types = [block['type'] for block in blocks]
        assert 'heading_2' in block_types, "Should have heading blocks"
        assert 'paragraph' in block_types, "Should have paragraph blocks"
        assert 'code' in block_types, "Should have code blocks"
        assert 'quote' in block_types, "Should have quote blocks"
        assert 'bulleted_list_item' in block_types, "Should have list blocks"
        
        print("✅ Markdown parsing test passed")
        
    finally:
        shutil.rmtree(vault_path)

def test_file_analysis():
    """Test file analysis and validation"""
    vault_path = create_test_vault()
    
    try:
        config = MigrationConfig(
            notion_token="fake_token",
            target_database_id="fake_database_id", 
            source_vault_path=str(vault_path),
            dry_run=True
        )
        
        migrator = ObsidianToNotionMigrator(config)
        
        # Test file analysis
        test_file = vault_path / "attachments" / "test_image.png"
        file_info = migrator._analyze_file(test_file)
        
        assert file_info.name == "test_image.png"
        assert file_info.size > 0
        assert file_info.hash is not None
        
        # Test file validation
        is_valid, error = migrator._validate_file_for_upload(file_info)
        assert is_valid, f"File should be valid: {error}"
        
        print("✅ File analysis test passed")
        
    finally:
        shutil.rmtree(vault_path)

def test_database_properties():
    """Test database property preparation"""
    vault_path = create_test_vault()
    
    try:
        config = MigrationConfig(
            notion_token="fake_token",
            target_database_id="fake_database_id", 
            source_vault_path=str(vault_path),
            dry_run=True
        )
        
        migrator = ObsidianToNotionMigrator(config)
        md_files = migrator._scan_vault()
        
        # Test property preparation
        note1 = next(f for f in md_files if f.path.name == "note1.md")
        properties = migrator._prepare_database_properties(note1)
        
        # Check required properties
        assert "Name" in properties, "Should have Name property"
        assert "Source File" in properties, "Should have Source File property"
        assert "Tags" in properties, "Should have Tags property"
        
        # Check frontmatter mapping
        assert "Author" in properties, "Should map author from frontmatter"
        assert "Priority" in properties, "Should map priority from frontmatter"
        
        print("✅ Database properties test passed")
        
    finally:
        shutil.rmtree(vault_path)

if __name__ == "__main__":
    print("Running Obsidian to Notion migration tests...")
    
    try:
        test_dry_run()
        test_markdown_parsing()
        test_file_analysis()
        test_database_properties()
        
        print("\n🎉 All tests passed!")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        raise