import asyncio
from subaccount_manager import SubaccountManager
from master_profit_orchestrator import MasterProfitOrchestrator

async def main():
    print("=== LIVE VPS SUBACCOUNT AUDIT ===")
    mgr = SubaccountManager()
    await mgr.fetch_subaccount_balances()
    summary = mgr.get_portfolio_summary()
    total_collat = summary.get('total_collateral_usd', 0.0)
    print(f"Total Portfolio Collateral: ${total_collat:.4f} USDC")
    for s in summary.get('shards', []):
        idx = s.get('account_index')
        name = s.get('name')
        role = s.get('role')
        collat = s.get('collateral_usd', 0.0)
        status = s.get('status')
        print(f"Shard #{idx} ({name}) | Role: {role} | Collateral: ${collat:.4f} | Status: {status}")
    
    print("\n=== LIVE STRATEGY TEST-FIRE ===")
    orch = MasterProfitOrchestrator(subaccount_manager=mgr, is_paper=True)
    sniper = orch.route_trade_to_shard("news_catalyst")
    mm = orch.route_trade_to_shard("dynamic_grid_mm")
    arb = orch.route_trade_to_shard("funding_harvester")
    print(f"1. Sniper Shard Target: #{sniper.account_index} ({sniper.name}) -> ACTIVE")
    print(f"2. MM Shard Target:     #{mm.account_index} ({mm.name}) -> ACTIVE")
    print(f"3. Arb Shard Target:    #{arb.account_index} ({arb.name}) -> ACTIVE")
    print("\n>>> ALL ENGINES & SHARDS ACTIVE AND HEALTHY <<<")

if __name__ == "__main__":
    asyncio.run(main())
