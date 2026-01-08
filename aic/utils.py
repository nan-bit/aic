import hashlib

def calculate_file_hash(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()
