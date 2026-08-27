from jupiter_trading.universe import Nifty50Universe


def test_parse_nifty_constituents_uses_isin_instrument_key() -> None:
    content = """Company Name,Industry,Symbol,Series,ISIN Code
Example Bank Ltd.,Financial Services,EXAMPLE,EQ,INE000A01001
Example Motors Ltd.,Automobile,EXMOTOR,EQ,INE000B01002
"""

    rows = Nifty50Universe.parse(content)

    assert rows == [
        {
            "symbol": "EXAMPLE",
            "instrument_key": "NSE_EQ|INE000A01001",
            "name": "Example Bank Ltd.",
            "industry": "Financial Services",
            "series": "EQ",
            "isin": "INE000A01001",
        },
        {
            "symbol": "EXMOTOR",
            "instrument_key": "NSE_EQ|INE000B01002",
            "name": "Example Motors Ltd.",
            "industry": "Automobile",
            "series": "EQ",
            "isin": "INE000B01002",
        },
    ]
