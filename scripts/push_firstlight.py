~/workspace$ # 1. Upload both KML and Excel to Railway
mkdir -p ~/workspace/firstlight
# Upload files via GitHub or direct copy

# 2. Pull and run push script
curl -H "Authorization: token $GITHUB_TOKEN" \
     -H "Accept: application/vnd.github.v3.raw" \
     -o ~/workspace/push_firstlight.py \
     "https://api.github.com/repos/azmartone67/dchub-backend/contents/push_firstlight.py"

python3 ~/workspace/push_firstlight.py

# 3. Pull seed script
curl -H "Authorization: token $GITHUB_TOKEN" \
     -H "Accept: application/vnd.github.v3.raw" \
     -o ~/workspace/firstlight_fiber_seed.py \
     "https://api.github.com/repos/azmartone67/dchub-backend/contents/firstlight_fiber_seed.py"

# 4. Copy data files to Railway workspace
cp "FLF Network 3-4-26 CONFIDENTIAL.kml" ~/workspace/firstlight/
cp "FLF Building List 1-26-26.xlsx" ~/workspace/firstlight/

# 5. Install deps and run
pip install openpyxl --break-system-packages
python3 ~/workspace/firstlight_fiber_seed.py
  % Total    % Received % Xferd  Average Speed   Time    Time     Time  Current
                                 Dload  Upload   Total   Spent    Left  Speed
100   141  100   141    0     0    749      0 --:--:-- --:--:-- --:--:--   750
  % Total    % Received % Xferd  Average Speed   Time    Time     Time  Current
                                 Dload  Upload   Total   Spent    Left  Speed
100   141  100   141    0     0    944      0 --:--:-- --:--:-- --:--:--   946
cp: cannot stat 'FLF Network 3-4-26 CONFIDENTIAL.kml': No such file or directory
cp: cannot stat 'FLF Building List 1-26-26.xlsx': No such file or directory
Requirement already satisfied: openpyxl in ./.pythonlibs/lib/python3.11/site-packages (3.1.5)
Requirement already satisfied: et-xmlfile in ./.pythonlibs/lib/python3.11/site-packages (from openpyxl) (2.0.0)
~/workspace$
