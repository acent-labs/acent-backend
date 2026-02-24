import asyncio
import os
import sys

import httpx

# Add the project root to the python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings

async def delete_all_documents(store_name: str):
    """Delete all documents in a fileSearchStore."""
    settings = get_settings()
    api_key = settings.gemini_api_key
    
    if not api_key:
        print("Error: GEMINI_API_KEY is not set.")
        return False

    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            # List all documents
            docs_url = f"https://generativelanguage.googleapis.com/v1beta/{store_name}/documents"
            print(f"Fetching documents from {store_name}...")
            response = await client.get(docs_url, headers=headers)
            
            if response.status_code != 200:
                print(f"✗ Failed to list documents: {response.status_code}")
                print(f"Response: {response.text}")
                return False
            
            data = response.json()
            documents = data.get("documents", [])
            print(f"Found {len(documents)} documents\n")
            
            if len(documents) == 0:
                print("No documents to delete.")
                return True
            
            # Delete each document
            success_count = 0
            fail_count = 0
            
            for i, doc in enumerate(documents, 1):
                doc_name = doc.get("name")
                if not doc_name:
                    continue
                
                print(f"[{i}/{len(documents)}] Deleting {doc_name}...")
                delete_url = f"https://generativelanguage.googleapis.com/v1beta/{doc_name}?force=true"
                delete_response = await client.delete(delete_url, headers=headers)
                
                if delete_response.status_code in [200, 204]:
                    print("  ✓ Deleted successfully")
                    success_count += 1
                else:
                    print(f"  ✗ Failed: {delete_response.status_code}")
                    print(f"  Response: {delete_response.text}")
                    fail_count += 1
                
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.5)
            
            print(f"\nResults: {success_count} succeeded, {fail_count} failed")
            return fail_count == 0
                
        except Exception as e:
            print(f"✗ Error: {e}")
            return False

async def main():
    if len(sys.argv) < 2:
        print("Usage: python delete_documents_from_store.py <store_name>")
        print("Example: python delete_documents_from_store.py fileSearchStores/freshworks-kb-with-metadata-azp7fvki3ejh")
        sys.exit(1)
    
    store_name = sys.argv[1]
    
    # Safety check
    if "g38ed5p7liay" in store_name:
        print("ERROR: Cannot delete documents from g38ed5p7liay store!")
        print("This store is protected and should not be modified.")
        sys.exit(1)
    
    print(f"=== Deleting documents from {store_name} ===\n")
    success = await delete_all_documents(store_name)
    
    if success:
        print("\n✓ All documents deleted successfully!")
    else:
        print("\n✗ Some documents could not be deleted.")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
