#!/usr/bin/env python3
"""Create all tables against DATABASE_URL. Run once against a fresh Postgres/Supabase database.

Usage: python scripts/init_db.py
"""
from webull_bot.db.session import create_all

if __name__ == "__main__":
    create_all()
    print("Tables created.")
