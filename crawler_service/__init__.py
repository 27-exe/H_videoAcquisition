"""crawler_service: HK-side per-request browser that returns URL lists to US bot.

Per plan §1: zero persistence, no DB touch, no state files committed.
Each request spins up one AsyncCamoufox instance, parses results, returns JSON.
"""
