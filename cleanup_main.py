#!/usr/bin/env python3
"""
DC Hub main.py Cleanup Script
Scans and fixes common code contamination issues in large Python files.

Usage:
    python cleanup_main.py main.py              # Dry run (report only)
    python cleanup_main.py main.py --fix        # Fix issues and write cleaned file
    python cleanup_main.py main.py --fix --backup  # Fix + create backup first
"""

import re
import sys
import shutil
from datetime import datetime
from pathlib import Path


class CodeCleaner:
    def __init__(self, filepath):
        self.filepath = Path(filepath)
        self.lines = []
        self.issues = []
        self.fixes_applied = 0

    def load(self):
        with open(self.filepath, 'r', encoding='utf-8', errors='replace') as f:
            self.lines = f.readlines()
        print(f"Loaded {len(self.lines)} lines from {self.filepath}")

    def backup(self):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = self.filepath.with_suffix(f'.backup_{timestamp}.py')
        shutil.copy2(self.filepath, backup_path)
        print(f"Backup created: {backup_path}")
        return backup_path

    # -------------------------------------------------------------------------
    # SCAN CHECKS
    # -------------------------------------------------------------------------

    def check_markdown_fences(self):
        """Find stray markdown code fences (```) that don't belong in Python."""
        for i, line in enumerate(self.lines):
            stripped = line.strip()
            # Match lines that are just ``` or ```python or ```json etc.
            if re.match(r'^```\w*$', stripped):
                self.issues.append({
                    'line': i + 1,
                    'type': 'MARKDOWN_FENCE',
                    'severity': 'HIGH',
                    'content': line.rstrip(),
                    'description': 'Stray markdown code fence',
                    'fix': 'remove_line'
                })

    def check_markdown_headers(self):
        """Find markdown headers (## etc.) outside of strings."""
        for i, line in enumerate(self.lines):
            stripped = line.strip()
            # Skip if inside a string (basic check - starts with quote or is in a docstring context)
            if stripped.startswith(('#!', '"', "'")) or stripped.startswith('# '):
                # Allow normal Python comments, but flag markdown-style headers
                if re.match(r'^#{2,6}\s+\w', stripped):
                    self.issues.append({
                        'line': i + 1,
                        'type': 'MARKDOWN_HEADER',
                        'severity': 'MEDIUM',
                        'content': line.rstrip(),
                        'description': 'Possible markdown header (## style) — verify it\'s a comment',
                        'fix': 'manual'
                    })

    def check_html_artifacts(self):
        """Find HTML/XML tags that may have been pasted from AI output."""
        html_patterns = [
            (r'<artifact', 'AI artifact tag'),
            (r'</artifact', 'AI artifact closing tag'),
            (r'<antartifact', 'AI artifact tag (misspelled)'),
            (r'<code_block', 'AI code block tag'),
            (r'</code_block', 'AI code block closing tag'),
        ]
        for i, line in enumerate(self.lines):
            stripped = line.strip()
            # Skip if clearly inside a string
            if stripped.startswith(("'", '"', '#')):
                continue
            for pattern, desc in html_patterns:
                if re.search(pattern, stripped, re.IGNORECASE):
                    self.issues.append({
                        'line': i + 1,
                        'type': 'HTML_ARTIFACT',
                        'severity': 'HIGH',
                        'content': line.rstrip(),
                        'description': desc,
                        'fix': 'remove_line'
                    })

    def check_indentation_anomalies(self):
        """Find lines with mixed tabs/spaces or sudden large indentation jumps."""
        prev_indent = 0
        for i, line in enumerate(self.lines):
            if not line.strip():  # skip blank lines
                continue

            # Mixed tabs and spaces
            if '\t' in line and ' ' in line[:len(line) - len(line.lstrip())]:
                leading = line[:len(line) - len(line.lstrip())]
                if '\t' in leading and ' ' in leading:
                    self.issues.append({
                        'line': i + 1,
                        'type': 'MIXED_INDENTATION',
                        'severity': 'HIGH',
                        'content': line.rstrip()[:100],
                        'description': 'Mixed tabs and spaces in indentation',
                        'fix': 'convert_tabs'
                    })

            # Large indentation jumps (more than 3 levels at once)
            current_indent = len(line) - len(line.lstrip())
            if current_indent - prev_indent > 12 and line.strip() and not line.strip().startswith('#'):
                self.issues.append({
                    'line': i + 1,
                    'type': 'INDENT_JUMP',
                    'severity': 'MEDIUM',
                    'content': line.rstrip()[:100],
                    'description': f'Large indentation jump ({prev_indent} -> {current_indent} spaces)',
                    'fix': 'manual'
                })
            if line.strip():
                prev_indent = current_indent

    def check_duplicate_functions(self):
        """Find duplicate function/route definitions."""
        func_defs = {}
        for i, line in enumerate(self.lines):
            # Match function definitions
            match = re.match(r'^(\s*)def\s+(\w+)\s*\(', line)
            if match:
                indent = len(match.group(1))
                name = match.group(2)
                if name in func_defs:
                    self.issues.append({
                        'line': i + 1,
                        'type': 'DUPLICATE_FUNCTION',
                        'severity': 'HIGH',
                        'content': line.rstrip(),
                        'description': f'Duplicate function "{name}" (first defined at line {func_defs[name]})',
                        'fix': 'manual'
                    })
                func_defs[name] = i + 1

    def check_duplicate_routes(self):
        """Find duplicate Flask route definitions."""
        routes = {}
        for i, line in enumerate(self.lines):
            match = re.match(r"""^\s*@app\.route\(\s*['"]([^'"]+)['"]""", line)
            if match:
                route = match.group(1)
                if route in routes:
                    self.issues.append({
                        'line': i + 1,
                        'type': 'DUPLICATE_ROUTE',
                        'severity': 'HIGH',
                        'content': line.rstrip(),
                        'description': f'Duplicate route "{route}" (first at line {routes[route]})',
                        'fix': 'manual'
                    })
                routes[route] = i + 1

    def check_trailing_ai_comments(self):
        """Find common AI-generated comment patterns that indicate pasted code."""
        ai_patterns = [
            r'#\s*(?:rest of|remaining)\s+(?:code|implementation)\s+(?:remains?\s+)?(?:the\s+)?same',
            r'#\s*\.\.\.\s*(?:existing|previous|rest)',
            r'#\s*(?:add|insert|paste)\s+(?:your|the)\s+(?:code|implementation)\s+here',
            r'#\s*TODO:\s*(?:implement|add|complete)\s+(?:this|the rest)',
        ]
        for i, line in enumerate(self.lines):
            stripped = line.strip()
            for pattern in ai_patterns:
                if re.search(pattern, stripped, re.IGNORECASE):
                    self.issues.append({
                        'line': i + 1,
                        'type': 'AI_PLACEHOLDER',
                        'severity': 'MEDIUM',
                        'content': line.rstrip(),
                        'description': 'AI-generated placeholder comment — code may be incomplete',
                        'fix': 'manual'
                    })
                    break

    def check_syntax_basics(self):
        """Quick check for obvious syntax problems."""
        for i, line in enumerate(self.lines):
            stripped = line.strip()

            # Unmatched triple quotes on non-docstring lines
            triple_single = stripped.count("'''")
            triple_double = stripped.count('"""')
            if (triple_single % 2 != 0) or (triple_double % 2 != 0):
                # Could be start/end of docstring — just flag it for awareness
                pass

            # Lines that are just a closing paren/bracket with wrong indent
            # (common copy-paste error)
            if stripped in (')', ']', '}') and i > 0:
                prev_indent = len(self.lines[i-1]) - len(self.lines[i-1].lstrip())
                curr_indent = len(line) - len(line.lstrip())
                if curr_indent > prev_indent + 8:
                    self.issues.append({
                        'line': i + 1,
                        'type': 'BRACKET_INDENT',
                        'severity': 'MEDIUM',
                        'content': line.rstrip(),
                        'description': 'Closing bracket with suspicious indentation',
                        'fix': 'manual'
                    })

    def check_unicode_issues(self):
        """Find problematic unicode characters that can cause silent failures."""
        problem_chars = {
            '\u200b': 'zero-width space',
            '\u200c': 'zero-width non-joiner',
            '\u200d': 'zero-width joiner',
            '\ufeff': 'BOM character',
            '\u00a0': 'non-breaking space',
            '\u2018': 'smart quote (left single)',
            '\u2019': 'smart quote (right single)',
            '\u201c': 'smart quote (left double)',
            '\u201d': 'smart quote (right double)',
            '\u2013': 'en-dash',
            '\u2014': 'em-dash',
        }
        for i, line in enumerate(self.lines):
            for char, name in problem_chars.items():
                if char in line:
                    # Check if it's inside a string (rough check)
                    col = line.index(char)
                    self.issues.append({
                        'line': i + 1,
                        'type': 'UNICODE',
                        'severity': 'MEDIUM',
                        'content': line.rstrip()[:100],
                        'description': f'Problematic unicode: {name} at col {col}',
                        'fix': 'fix_unicode'
                    })

    # -------------------------------------------------------------------------
    # FIX METHODS
    # -------------------------------------------------------------------------

    def apply_fixes(self):
        """Apply automatic fixes for HIGH severity auto-fixable issues."""
        lines_to_remove = set()
        lines_to_fix_tabs = set()
        lines_to_fix_unicode = set()

        for issue in self.issues:
            if issue['fix'] == 'remove_line':
                lines_to_remove.add(issue['line'] - 1)  # 0-indexed
            elif issue['fix'] == 'convert_tabs':
                lines_to_fix_tabs.add(issue['line'] - 1)
            elif issue['fix'] == 'fix_unicode':
                lines_to_fix_unicode.add(issue['line'] - 1)

        new_lines = []
        for i, line in enumerate(self.lines):
            if i in lines_to_remove:
                self.fixes_applied += 1
                continue  # skip this line entirely

            if i in lines_to_fix_tabs:
                line = line.replace('\t', '    ')
                self.fixes_applied += 1

            if i in lines_to_fix_unicode:
                # Replace smart quotes with regular ones
                line = line.replace('\u2018', "'").replace('\u2019', "'")
                line = line.replace('\u201c', '"').replace('\u201d', '"')
                line = line.replace('\u2013', '-').replace('\u2014', '--')
                line = line.replace('\u00a0', ' ')
                # Remove zero-width characters
                for zw in ['\u200b', '\u200c', '\u200d', '\ufeff']:
                    line = line.replace(zw, '')
                self.fixes_applied += 1

            new_lines.append(line)

        self.lines = new_lines

    def save(self):
        """Write cleaned file."""
        with open(self.filepath, 'w', encoding='utf-8') as f:
            f.writelines(self.lines)
        print(f"Saved cleaned file: {self.filepath} ({len(self.lines)} lines)")

    # -------------------------------------------------------------------------
    # MAIN SCAN & REPORT
    # -------------------------------------------------------------------------

    def scan(self):
        """Run all checks."""
        print("\nScanning for issues...\n")
        self.check_markdown_fences()
        self.check_markdown_headers()
        self.check_html_artifacts()
        self.check_indentation_anomalies()
        self.check_duplicate_functions()
        self.check_duplicate_routes()
        self.check_trailing_ai_comments()
        self.check_syntax_basics()
        self.check_unicode_issues()

        # Sort by line number
        self.issues.sort(key=lambda x: x['line'])

    def report(self):
        """Print a summary report."""
        if not self.issues:
            print("✅ No issues found!")
            return

        # Group by severity
        high = [i for i in self.issues if i['severity'] == 'HIGH']
        medium = [i for i in self.issues if i['severity'] == 'MEDIUM']

        print(f"{'='*70}")
        print(f"SCAN RESULTS: {len(self.issues)} issues found")
        print(f"  🔴 HIGH: {len(high)}  (auto-fixable)")
        print(f"  🟡 MEDIUM: {len(medium)}  (review manually)")
        print(f"{'='*70}\n")

        # Group by type for summary
        type_counts = {}
        for issue in self.issues:
            t = issue['type']
            type_counts[t] = type_counts.get(t, 0) + 1

        print("Issue Summary:")
        for itype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"  {itype}: {count}")
        print()

        # Detail
        for issue in self.issues:
            severity_icon = '🔴' if issue['severity'] == 'HIGH' else '🟡'
            fix_label = '[AUTO-FIX]' if issue['fix'] != 'manual' else '[MANUAL]'
            print(f"  {severity_icon} Line {issue['line']:>6}: {issue['type']} {fix_label}")
            print(f"           {issue['description']}")
            print(f"           {issue['content'][:80]}")
            print()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    filepath = sys.argv[1]
    do_fix = '--fix' in sys.argv
    do_backup = '--backup' in sys.argv

    cleaner = CodeCleaner(filepath)
    cleaner.load()
    cleaner.scan()
    cleaner.report()

    if do_fix:
        if do_backup:
            cleaner.backup()

        print(f"\nApplying automatic fixes...")
        cleaner.apply_fixes()
        cleaner.save()
        print(f"✅ {cleaner.fixes_applied} fixes applied.")

        # Verify syntax after fix
        print("\nVerifying Python syntax...")
        try:
            with open(filepath, 'r') as f:
                compile(f.read(), filepath, 'exec')
            print("✅ Syntax check passed!")
        except SyntaxError as e:
            print(f"❌ Syntax error remains: {e}")
            print(f"   Line {e.lineno}: {e.text}")
            print("   Manual review needed for this issue.")

        manual_issues = [i for i in cleaner.issues if i['fix'] == 'manual']
        if manual_issues:
            print(f"\n⚠️  {len(manual_issues)} issues require manual review (see report above)")
    else:
        auto_fixable = len([i for i in cleaner.issues if i['fix'] != 'manual'])
        if auto_fixable:
            print(f"\n💡 Run with --fix to auto-fix {auto_fixable} issues:")
            print(f"   python cleanup_main.py {filepath} --fix --backup")


if __name__ == '__main__':
    main()
