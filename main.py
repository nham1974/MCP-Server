from fastmcp import FastMCP  # create a FastMCP instance
import httpx  # import httpx for making HTTP requests asynchronouesly
from starlette.responses import JSONResponse # import JSONResponse for sending JSON responses
import uvicorn # import uvicorn for running the server "asgi" :->  asynchronous server gateway interface 
import json
# FastMCP server configuration
mcp = FastMCP("mcp-exchangerates")  # create a FastMCP instance with the name "mcp-exchangerates"

# Create a Starlette ASGI web application for uvicorn to serve
# in order to use the FastMCP instance as an ASGI application, we need a web application to serve it. 
# We can use the http_app() method to create a Starlette ASGI web application for uvicorn to serve.
app = mcp.http_app() # create an HTTP application for the FastMCP instance

@mcp.tool()
async def listCurrencies():
    """
    Lists all foreign currencies available for conversion in ISO 4217 currency code format
    
    Returns:
        foreign currency codes list
    """
    r = httpx.get("https://bcd-api-dca-ipa.cbsa-asfc.cloud-nuage.canada.ca/exchange-rate-lambda/exchange-rates")
    rates = json.loads(r.text)["ForeignExchangeRates"]
    currencies = [x['FromCurrency']['Value'] for x in rates] 
    return sorted(currencies)

async def getExchangeRate(currencyCode: str):

    #get rates from Bank Of Canada API
    r = httpx.get("https://bcd-api-dca-ipa.cbsa-asfc.cloud-nuage.canada.ca/exchange-rate-lambda/exchange-rates")
    rates = json.loads(r.text)["ForeignExchangeRates"]
    
    # find rate using FromCurrency field
    for rate in rates:
        if rate["FromCurrency"]["Value"] == currencyCode:
            return float(rate['Rate'])

    return None

@mcp.tool()
async def convertToCanadianDollars(amount: float, currencyCode: str):
    """
    converts a given amount of Canadian currency to a target currency

    Args:
        amount: The amount of Canadian currency to be converted to the target currency (e.g. 500.23)
        currencyCode: The ISO 4217 currency code for the target currency (e.g. "USD")
    
    Returns:
        target currency amount
    """
    rate = await getExchangeRate(currencyCode)
    convertedAmount = amount / rate
    return round(convertedAmount, 2)

@mcp.tool(description="Get real-time exchange rate between two currencies")
async def get_exchange_rate(base: str, target: str) -> float:
    url = f"https://open.er-api.com/v6/latest/{base}"

    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        data = response.json()

    if target not in data["rates"]:
        raise ValueError(f"Currency {target} not found")

    return data["rates"][target]
# Add health check endpoint for Azure Container Apps
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "healthy", "service": "mcp-server"})

# we need to start a web server hosted on 0.0.0.0   and listening on port 8000 to serve the FastMCP instance.
if __name__ == "__main__":
    #uvicorn.run(app, host="0.0.0.0", port=8001) # start the uvicorn server to serve the FastMCP instance on port 8000
    mcp.run(
        transport="sse",
        host="0.0.0.0",
        port=8000

    ) # to run using LLM studio in stdio mode ..
"""
     import asyncio
   result = asyncio.run(listCurrencies.fn())
   print(result)
import asyncio
result = asyncio.run(convertToCanadianDollars.fn(100, 'USD'))
print(result)
"""