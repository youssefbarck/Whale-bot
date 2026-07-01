def get_prices():
    if "prices" in _cache and time.time() - _cache["prices"][0] < 120:
        return _cache["prices"][1]
    
    result = {}
    try:
        ids = ",".join(c["id"] for c in COINS.values())
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ids,
                "sparkline": "false",
                "price_change_percentage": "24h"
            },
            timeout=15,
            headers=HEADERS
        )
        if r.status_code == 200:
            for coin in r.json():
                sym = coin.get("symbol", "").upper()
                for code, info in COINS.items():
                    if info["id"] == coin.get("id"):
                        result[code] = {
                            "price": coin.get("current_price", 0),
                            "change_24h": coin.get("price_change_percentage_24h", 0) or 0
                        }
                        break
    except:
        pass
    
    # Fallback: Coinbase للأسعار فقط
    for code in COINS:
        if code not in result or result[code]["price"] == 0:
            try:
                r = requests.get(
                    f"https://api.coinbase.com/v2/prices/{code}-USD/spot",
                    timeout=10,
                    headers=HEADERS
                )
                if r.status_code == 200:
                    result[code] = {
                        "price": float(r.json()["data"]["amount"]),
                        "change_24h": 0
                    }
            except:
                pass
    
    if result:
        _cache["prices"] = (time.time(), result)
    return result
