import asyncio
import os
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        base_url = "https://white-cliff-0bca3ed00.1.azurestaticapps.net"
        
        await page.goto(f"{base_url}/login")
        await page.fill("#email", "admin@gmail.com")
        await page.fill("#password", "password")
        await page.click("[data-testid='btn-login']")
        await page.wait_for_url("**/dashboard/my-applications")
        await page.wait_for_timeout(2000)
        
        await page.click("[data-testid='btn-new-application']")
        await page.wait_for_timeout(1000)
        
        # Step 1: Select Type
        await page.click("[data-testid*='waiver-type-radio-2a7afc48']")
        await page.click("[data-testid='btn-next']")
        await page.wait_for_timeout(1000)
        
        # Step 2: Search Facility
        search_input = page.locator("[data-testid='input-facility-search']")
        await search_input.click()
        await search_input.fill("California")
        await page.wait_for_timeout(1000)
        await page.click("[data-testid='facility-option-0']", force=True)
        await page.wait_for_timeout(500)
        
        # Click facility type select
        type_select = page.locator("[data-testid='input-facility-type']")
        await type_select.click(force=True)
        await page.wait_for_timeout(1000)
        await page.click("[data-testid*='facility-type-option-']", force=True)
        await page.wait_for_timeout(1000)
        
        # Click Next
        next_btn = page.locator("[data-testid='btn-next']")
        print("Next button disabled before click?", await next_btn.is_disabled())
        await next_btn.click()
        
        # Wait 5 full seconds for Step 3 to load
        print("Waiting 5 seconds for Step 3...")
        await page.wait_for_timeout(5000)
        
        print("URL after 5 seconds:", page.url)
        
        # Print Step 3 elements
        print("\nStep 3 Elements:")
        elements = await page.locator("[data-testid]").all()
        for elem in elements:
            testid = await elem.get_attribute("data-testid")
            text = (await elem.inner_text()).strip().replace('\n', ' ')[:100]
            print(f"  testid='{testid}' text='{text}'")
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
