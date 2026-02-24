import asyncio
import os
import sys

import httpx

# Add the project root to the python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings

async def delete_store(store_name: str):
    settings = get_settings()
    api_key = settings.gemini_api_key
    
    if not api_key:
        print("Error: GEMINI_API_KEY is not set.")
        return

    # Extract corpus name from fileSearchStore name
    # fileSearchStores/freshworks-kb-with-metadata-lsbfqk1sqp2i -> corpora/freshworks-kb-with-metadata-lsbfqk1sqp2i
    if store_name.startswith("fileSearchStores/"):
        corpus_id = store_name.replace("fileSearchStores/", "")
        corpus_name = f"corpora/{corpus_id}"
    else:
        corpus_name = store_name
    
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient() as client:
        # Delete the corpus
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/{corpus_name}"
            print(f"Deleting {corpus_name}...")
            response = await client.delete(url, headers=headers)
            
            if response.status_code == 200:
                print(f"✓ Successfully deleted {corpus_name}")
                print(f"Response: {response.json()}")
            elif response.status_code == 204:
                print(f"✓ Successfully deleted {corpus_name} (no content)")
            else:
                print(f"✗ Failed to delete {corpus_name}")
                print(f"Status: {response.status_code}")
                print(f"Response: {response.text}")
        except Exception as e:
            print(f"✗ Error deleting {corpus_name}: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python delete_gemini_store.py <store_name>")
        print("Example: python delete_gemini_store.py fileSearchStores/freshworks-kb-with-metadata-lsbfqk1sqp2i")
        sys.exit(1)
    
    store_name = sys.argv[1]
    asyncio.run(delete_store(store_name))
