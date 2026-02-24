import asyncio
from app.core.config import get_settings
from app.services.gemini_file_search import get_store_documents

async def check_stores():
    settings = get_settings()
    ticket_store = getattr(settings, "gemini_store_tickets", None)
    article_store = getattr(settings, "gemini_store_articles", None)
    
    print("Checking stores...")
    print(f"Ticket Store: {ticket_store}")
    print(f"Article Store: {article_store}")
    
    if ticket_store:
        try:
            docs = await get_store_documents(ticket_store)
            count = len(docs.get("documents", []))
            print(f"Ticket Store Documents: {count}")
        except Exception as e:
            print(f"Error checking ticket store: {e}")

    if article_store:
        try:
            docs = await get_store_documents(article_store)
            count = len(docs.get("documents", []))
            print(f"Article Store Documents: {count}")
        except Exception as e:
            print(f"Error checking article store: {e}")

if __name__ == "__main__":
    asyncio.run(check_stores())
