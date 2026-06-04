import asyncio
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test")

async def mock_sheet_writer(r_q, total):
    success, fail = 0, 0
    updates = []
    last_flush = time.time()
    
    async def _flush_to_sheets():
        nonlocal updates, last_flush
        if not updates: return
        logger.info(f"FLUSHING {len(updates)} updates!")
        await asyncio.sleep(0.5) # Simulate network
        for _ in updates:
            r_q.task_done()
        updates = []
        last_flush = time.time()
        
    try:
        while True:
            try:
                item = await asyncio.wait_for(r_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            else:
                items = [item]
                while not r_q.empty() and len(items) < 50:
                    items.append(r_q.get_nowait())
                
                for i in items:
                    if isinstance(i, dict):
                        if i.get('type') == 'progress':
                            success += 1
                            r_q.task_done()
                        else:
                            updates.append(i)
                            logger.info(f"Appended item {i}")
            
            if updates and (len(updates) >= 10 or time.time() - last_flush > 5):
                await _flush_to_sheets()
            if success == total:
                logger.info("All done natively!")
    except asyncio.CancelledError:
        logger.info("CANCELLED! Flushing remaining...")
        if updates:
            await _flush_to_sheets()
        raise

async def main():
    q = asyncio.Queue()
    writer = asyncio.create_task(mock_sheet_writer(q, total=10))
    
    # Push some items
    for i in range(3):
        await q.put({'range': f'A{i}', 'values': [[i]]})
        await q.put({'type': 'progress'})
        
    # Wait a bit
    await asyncio.sleep(2)
    
    # Cancel the writer early!
    logger.info("Cancelling writer!")
    writer.cancel()
    
    try:
        await writer
    except asyncio.CancelledError:
        pass
        
    logger.info("Joining queue...")
    # This will hang if task_done wasn't called for the 3 range items!
    await asyncio.wait_for(q.join(), timeout=2.0)
    logger.info("Queue joined successfully! No deadlock!")

if __name__ == "__main__":
    asyncio.run(main())
