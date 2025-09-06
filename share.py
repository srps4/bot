import os
from dotenv import load_dotenv
import dropbox
from dropbox import exceptions as E

# Load .env (APP KEY/SECRET/REFRESH TOKEN)
load_dotenv()

dbx = dropbox.Dropbox(
    app_key=os.getenv("DBX_APP_KEY"),
    app_secret=os.getenv("DBX_APP_SECRET"),
    oauth2_refresh_token=os.getenv("DBX_REFRESH_TOKEN"),
)

# Change this to any path inside your App folder
PATH = "/hello.txt"

def ensure_link(p: str) -> str:
    try:
        return dbx.sharing_create_shared_link_with_settings(p).url
    except E.ApiError:
        links = dbx.sharing_list_shared_links(path=p, direct_only=True).links
        if links:
            return links[0].url
        raise

url = ensure_link(PATH)
# Force direct download style if you want
print(url.replace("?dl=0", "?dl=1"))
