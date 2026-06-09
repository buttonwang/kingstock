import os, re

fname = 'data/output/v1_7_backtest.log'
size = os.path.getsize(fname)
f = open(fname, 'rb')
data = f.read()
f.close()

# Find all progress markers like "回测 216/726 (29%)"
text = data.decode('gbk', errors='replace')
lines = text.split('\n')
for line in lines:
    if '/726' in line or '总信号' in line or '开始 V1.7' in line or '开始 V1.0' in line or 'V1.7 信号' in line:
        print(line.strip())

print(f"\nFile size: {size} bytes")
print(f"Last few lines:")
for line in lines[-5:]:
    print(line.strip()[:120])

# Check if output file exists
if os.path.exists('data/output/66dashun_v1_7_curve.png'):
    print("\nOUTPUT FILE EXISTS!")
    st = os.stat('data/output/66dashun_v1_7_curve.png')
    print(f"  Size: {st.st_size}")
else:
    print("\nOutput file not yet created")
