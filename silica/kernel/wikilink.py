import re

NON_MD_EXTENSIONS = (
    '.png', '.jpg', '.jpeg', '.pdf', '.webp', '.svg', '.gif', '.mp4', '.zip', '.html', '.css', '.js'
)

def extract_links(content: str) -> list[str]:
    """Extract clean wikilinks (both [[target]] and ![[target]]) from note content.

    Strips:
      - Multi-line code fence blocks (```...```)
      - Inline code (`...`)
      - Targets ending with non-markdown extensions (e.g. .png, .jpg)
      - Aliases/headers within links (e.g. [[Target#Header|Alias]] -> Target)
    """
    # 1. Strip multi-line code fence blocks (```...```)
    content_no_code = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
    
    # 2. Strip inline code (`...`)
    content_no_code = re.sub(r'`[^`\n]+`', '', content_no_code)
    
    # 3. Find all wikilinks (including embeds with !)
    # Regex matches both [[target]] and ![[target]]
    raw_targets = re.findall(r'\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]', content_no_code)
    
    # 4. Clean, filter non-markdown, and return unique targets
    cleaned = []
    for t in raw_targets:
        t = t.strip()
        if not t:
            continue
        if t.lower().endswith(NON_MD_EXTENSIONS):
            continue
        cleaned.append(t)
    return cleaned
