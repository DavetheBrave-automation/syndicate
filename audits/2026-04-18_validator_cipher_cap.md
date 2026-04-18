# Validator: CIPHER hard execution cap (10 contracts)
**Date:** 2026-04-18  
**Fix:** `main.py` `_act_on_decision` — CIPHER positions capped at `10 * contract_price` dollars before quantity calculation. Enforced at execution layer regardless of TC bet_size.

## Test Script

```python
def apply_cipher_cap(agent_name, size_dollars, contract_price):
    if agent_name == "CIPHER" and contract_price > 0:
        cipher_max_dollars = max(1, int(10 * contract_price))
        if size_dollars > cipher_max_dollars:
            # log: [Gate] CIPHER hard cap applied: $X → $Y
            return cipher_max_dollars, True
    return size_dollars, False

def compute_quantity(size_dollars, contract_price):
    return max(1, int(size_dollars / contract_price)) if contract_price > 0 else 1
```

## Test Cases and Results

| Case | Agent | Bet | Price | Exp Size | Exp Qty | Capped | Result |
|------|-------|-----|-------|----------|---------|--------|--------|
| CIPHER bet=100 price=0.60 | CIPHER | 100 | 0.60 | $6 | 10 | Yes | PASS |
| CIPHER bet=100 price=0.30 | CIPHER | 100 | 0.30 | $3 | 10 | Yes | PASS |
| CIPHER bet=5 price=0.50 | CIPHER | 5 | 0.50 | $5 | 10 | No | PASS |
| CIPHER bet=6 price=0.60 | CIPHER | 6 | 0.60 | $6 | 10 | No | PASS |
| CIPHER bet=7 price=0.60 | CIPHER | 7 | 0.60 | $6 | 10 | Yes | PASS |
| AXIOM bet=100 price=0.25 | AXIOM | 100 | 0.25 | $100 | 400 | No | PASS |
| DELTA bet=50 price=0.40 | DELTA | 50 | 0.40 | $50 | 125 | No | PASS |
| CIPHER bet=100 price=0.10 | CIPHER | 100 | 0.10 | $1 | 10 | Yes | PASS |

## Cap Log Lines Fired

```
[Gate] CIPHER hard cap applied: $100 → $6 (10-contract limit, 0.60/contract)
[Gate] CIPHER hard cap applied: $100 → $3 (10-contract limit, 0.30/contract)
[Gate] CIPHER hard cap applied: $7 → $6 (10-contract limit, 0.60/contract)
[Gate] CIPHER hard cap applied: $100 → $1 (10-contract limit, 0.10/contract)
```

## Result: ALL PASS

## Notes
- Cap is hard at execution layer — TC's bet_size cannot exceed 10 contracts
- AXIOM and DELTA are NOT capped (cap is CIPHER-only)
- At-cap or under-cap signals pass through unchanged (no false cap fires)
- Remove cap when CIPHER edge validated at >10 contract scale (refs audits/2026-04-18_deep_excavation.md)
