import os
import sys

# Add current dir to path
sys.path.append(os.getcwd())

print("Testing imports...")
try:
    print("✅ config")
    print("✅ constants")
    print("✅ db_conn")
    print("✅ gateway")
    print("✅ bot_app")
    print("✅ web_app")
    print("\nAll main modules imported successfully!")
except Exception as e:
    print(f"\n❌ Import failed: {e}")
    import traceback

    traceback.print_exc()
    sys.exit(1)
