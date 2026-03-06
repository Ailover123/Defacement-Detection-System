import re
import sys
import os

# Configuration
TARGET_TABLE = "defacement_sites"

def transform_siteid(old_siteid):
    """
    Logic:
    - 10108 -> siteid: 10108, baseline_id: 10108-1
    - 10108-1 -> siteid: 10108, baseline_id: 10108-2
    """
    old_siteid = old_siteid.strip("'\"")
    if not old_siteid or old_siteid.lower() == 'null':
        return 'NULL', 'NULL'
        
    if '-' in old_siteid:
        # It's a child url
        parts = old_siteid.split('-')
        prefix = parts[0]
        try:
            suffix = int(parts[1])
            new_siteid = prefix
            new_baseline_id = f"{prefix}-{suffix + 1}"
        except ValueError:
            # Fallback if suffix is not a number
            new_siteid = prefix
            new_baseline_id = f"{old_siteid}-1"
    else:
        # It's a parent domain
        new_siteid = old_siteid
        new_baseline_id = f"{old_siteid}-1"
    
    return f"'{new_siteid}'", f"'{new_baseline_id}'"

def split_sql_row(row_content):
    """
    Splits row content by comma, correctly handling quoted strings and escaped backslashes.
    """
    values = []
    current = []
    in_string = False
    quote_char = None
    escaped = False
    
    i = 0
    while i < len(row_content):
        char = row_content[i]
        if escaped:
            current.append(char)
            escaped = False
        elif char == '\\':
            current.append(char)
            escaped = True
        elif char in ("'", '"'):
            if not in_string:
                in_string = True
                quote_char = char
                current.append(char)
            elif char == quote_char:
                # Handle double-quote escape ''
                if i + 1 < len(row_content) and row_content[i+1] == quote_char:
                    current.append(char)
                    current.append(char)
                    i += 1
                else:
                    in_string = False
                    quote_char = None
                    current.append(char)
            else:
                current.append(char)
        elif char == ',' and not in_string:
            values.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        i += 1
            
    values.append("".join(current).strip())
    return values

def extract_rows(values_content):
    """
    Extracts individual (row1), (row2) blocks, correctly handling parens in strings.
    """
    rows = []
    current_row = []
    paren_depth = 0
    in_string = False
    quote_char = None
    escaped = False
    
    i = 0
    while i < len(values_content):
        char = values_content[i]
        if escaped:
            current_row.append(char)
            escaped = False
        elif char == '\\':
            current_row.append(char)
            escaped = True
        elif char in ("'", '"'):
            if not in_string:
                in_string = True
                quote_char = char
            elif char == quote_char:
                # Handle double-quote escape ''
                if i + 1 < len(values_content) and values_content[i+1] == quote_char:
                    current_row.append(char)
                    current_row.append(char)
                    i += 1
                else:
                    in_string = False
                    quote_char = None
            current_row.append(char)
        elif char == '(' and not in_string:
            if paren_depth == 0:
                current_row = []
            current_row.append(char)
            paren_depth += 1
        elif char == ')' and not in_string:
            paren_depth -= 1
            current_row.append(char)
            if paren_depth == 0:
                rows.append("".join(current_row))
                current_row = []
        elif paren_depth > 0:
            current_row.append(char)
        i += 1
            
    return rows

def split_sql_statements(content):
    """
    Splits SQL content into statements, correctly handling semicolons in strings.
    """
    statements = []
    current_stmt = []
    in_string = False
    quote_char = None
    escaped = False
    
    i = 0
    while i < len(content):
        char = content[i]
        if escaped:
            current_stmt.append(char)
            escaped = False
        elif char == '\\':
            current_stmt.append(char)
            escaped = True
        elif char in ("'", '"'):
            if not in_string:
                in_string = True
                quote_char = char
            elif char == quote_char:
                if i + 1 < len(content) and content[i+1] == quote_char:
                    current_stmt.append(char)
                    current_stmt.append(char)
                    i += 1
                else:
                    in_string = False
                    quote_char = None
            current_stmt.append(char)
        elif char == ';' and not in_string:
            current_stmt.append(char)
            statements.append("".join(current_stmt))
            current_stmt = []
        else:
            current_stmt.append(char)
        i += 1
    
    if current_stmt:
        statements.append("".join(current_stmt))
    
    return statements

def migrate_sql(input_file, output_file):
    print(f"Migrating {input_file} to {output_file}...")
    
    if not os.path.exists(input_file):
        print(f"Error: Input file {input_file} not found.")
        return

    with open(input_file, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    processed_statements = []

    # 2. Split into statements and process
    statements = split_sql_statements(content)
    
    for stmt in statements:
        # --- TRANSFORMATION LOGIC ---

        # 1. Rename only the table name (using word boundaries \b)
        # This keeps index names like idx_defacement_sites_url exactly as they are.
        stmt = re.sub(r"\bdefacement_sites\b", TARGET_TABLE, stmt, flags=re.IGNORECASE)

        # 2. Process INSERT statements
        if re.search(rf"INSERT INTO `{TARGET_TABLE}`", stmt, flags=re.IGNORECASE):
            # A. Add baseline_id to column list
            insert_head_pattern = r"(INSERT INTO `[^`]+` \([^)]*`siteid`)((?:, [^)]*)?\))"
            stmt = re.sub(insert_head_pattern, r"\1, `baseline_id`\2", stmt, flags=re.IGNORECASE)
            
            # B. Process VALUES
            match = re.search(r"(.*?VALUES)(.*);", stmt, flags=re.IGNORECASE | re.DOTALL)
            if match:
                header = match.group(1)
                values_str = match.group(2)
                
                rows = extract_rows(values_str)
                processed_rows = []
                for row in rows:
                    inner_content = row[1:-1] # Strip (...)
                    values = split_sql_row(inner_content)
                    
                    # siteid is at index 7 (id, url, group_id, email, email_cc1, email_cc2, action, siteid)
                    siteid_idx = 7

                    if len(values) > siteid_idx:
                        old_siteid = values[siteid_idx].strip()
                        new_siteid_val, new_baseline_id_val = transform_siteid(old_siteid)
                        
                        values[siteid_idx] = " " + new_siteid_val
                        values.insert(siteid_idx + 1, " " + new_baseline_id_val)
                        processed_rows.append("(" + ",".join(values) + ")")
                    else:
                        print(f"Warning: Skipping row transformation due to insufficient columns: {row[:50]}...")
                        processed_rows.append(row)
                
                stmt = header + "\n" + ",\n".join(processed_rows) + ";"
        
        # 3. Process CREATE TABLE statement
        elif re.search(rf"CREATE TABLE `{TARGET_TABLE}`", stmt, flags=re.IGNORECASE):

            # 1️⃣ Ensure id column is AUTO_INCREMENT
            stmt = re.sub(
                r"`id`\s+int\(?\d*\)?\s+NOT NULL",
                "`id` int NOT NULL AUTO_INCREMENT",
                stmt,
                flags=re.IGNORECASE
            )

            # 2️⃣ Add baseline_id column after siteid if not already added
            table_pattern = r"(`siteid`\s+varchar\(100\)\s+NOT NULL,)"
            if re.search(table_pattern, stmt, flags=re.IGNORECASE):
                stmt = re.sub(
                    table_pattern,
                    r"\1\n  `baseline_id` varchar(100) DEFAULT NULL,",
                    stmt,
                    flags=re.IGNORECASE
                )

            # 3️⃣ Ensure PRIMARY KEY exists
            if not re.search(r"PRIMARY KEY\s*\(`id`\)", stmt, flags=re.IGNORECASE):

                # Add PRIMARY KEY before closing parenthesis
                stmt = re.sub(
                    r"\)\s*ENGINE=",
                    ",\n  PRIMARY KEY (`id`)\n) ENGINE=",
                    stmt,
                    flags=re.IGNORECASE
                )

        processed_statements.append(stmt)

    with open(output_file, 'w', encoding='utf-8') as f:
        # Handle database renaming in comments
        output_content = "".join(processed_statements)
        output_content = output_content.replace("Database: `devadminsitewall_waf`", "Database: `devadminsitewall_waf_migrate`")
        f.write(output_content)
    
    print(f"Migration complete! Output saved to {output_file}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        input_path = sys.argv[1]
        output_path = sys.argv[2] if len(sys.argv) > 2 else input_path.replace(".sql", "_migrated.sql")
    else:
        input_path = "/home/devadminsitewall/defacement/defacement_sites_04_03_2026.sql"
        output_path = "/home/devadminsitewall/intern/Defacement-Detection-System-merger/defacement_sites_dev_migrated.sql"
    
    migrate_sql(input_path, output_path)
