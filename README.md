# Kujata flip board (Vercel)

public/index.html  the dashboard page
app.py            Flask backend that does the Universalis scan
requirements.txt  requests

## Deploy (GitHub route, easiest)
1. Create a free account at github.com, then a new repository (e.g. kujata-dashboard).
2. Upload these four files/folders to it (drag and drop in the browser works).
3. Go to vercel.com, sign in with GitHub, press Add New > Project, pick the repo, press Deploy.
   No settings need changing. You get a link like kujata-dashboard.vercel.app.

## Deploy (command line route)
npm i -g vercel
cd kujata-dashboard
vercel          # follow the prompts, then: vercel --prod

## Notes
- Hobby plan functions may be capped below 60s; if a scan times out, remove some items.
- Prices only exist for items players recently uploaded; very quiet items show gaps.
