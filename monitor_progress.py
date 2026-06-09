import os, time, sys

fname = 'data/output/v1_7_backtest.log'
outname = 'data/output/66dashun_v1_7_curve.png'
debugname = 'data/output/v1_7_signals_debug.csv'

prev_size = 0
for i in range(180):  # Check every 20 seconds for up to 60 minutes
    time.sleep(20)
    try:
        size = os.path.getsize(fname)
    except:
        print(f"[{time.strftime('%H:%M:%S')}] Log file not found")
        continue
    
    added = size - prev_size
    prev_size = size
    
    # Read last progress
    try:
        f = open(fname, 'rb')
        data = f.read()
        f.close()
        text = data.decode('utf-16-le', errors='replace')
        lines = text.split('\n')
        progress_lines = [l for l in lines if '/726' in l]
        last_progress = progress_lines[-1].strip() if progress_lines else 'N/A'
    except:
        last_progress = 'N/A'
    
    out_exists = os.path.exists(outname)
    debug_exists = os.path.exists(debugname)
    
    extra = ""
    if out_exists: extra += " [CURVE_PNG]"
    if debug_exists: extra += f" [SIGNALS_CSV:{os.path.getsize(debugname)}b]"
    
    print(f"[{time.strftime('%H:%M:%S')}] Size:{size:>8} +{added:>6} | {last_progress}{extra}")
    
    if out_exists:
        print("\n=== BACKTEST COMPLETE! ===")
        sys.exit(0)

print("\n=== Timeout reached ===")
