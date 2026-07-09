import asyncio
from sr_common.supabase_client import fetch_scraped_content

async def main():
    print("Testing supabase fetch...")
    content = await fetch_scraped_content("aarthikfinserv.com")
    if content:
        print(f"SUCCESS: Fetched {len(content)} characters.")
    else:
        print("FAILED: Returned None.")

if __name__ == "__main__":
    asyncio.run(main())
