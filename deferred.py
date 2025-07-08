def search_and_alert():
    found_something = False
    headers = {
        "Accept": "application/vnd.github.v3+json", # Standard header for GitHub API
        "Authorization": f"token {GITHUB_TOKEN}",  # Authenticate with your PAT
        "User-Agent": "DeFiRepoBot/1.0" # Good practice: identify your bot
    }

    for kw in KEYWORDS:
        # Construct the GitHub API URL for searching repositories
        # q: query string, combines keyword and created date filter
        # sort: how to sort results (e.g., 'updated', 'stars', 'forks')
        # order: sort direction ('desc' for descending, 'asc' for ascending)
        # per_page: number of results per page (max 100 for search API)
        github_api_url = (
            f"https://api.github.com/search/repositories?"
            f"q={kw}+created:>{GITHUB_SINCE}&" # Correct API date filter syntax
            f"sort=updated&order=desc&"      # Sort by last updated, newest first
            f"per_page=3"                     # Get top 3 results per keyword
        )

        print(f"Searching for: {kw} at URL: {github_api_url}") # For debugging

        try:
            res = requests.get(github_api_url, headers=headers)
            res.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)
            data = res.json()      # Parse the JSON response

            repos = data.get('items', []) # GitHub API search results are in the 'items' array

            if repos:
                found_something = True

            for repo in repos:
                # Extract relevant info from the API response
                name = repo['full_name'] # e.g., 'owner/repo-name'
                link = repo['html_url']   # The URL to the repository on github.com
                description = repo.get('description', 'No description provided.') # Use .get() to avoid KeyError if description is missing

                send(f"🔥 New {kw} repo:\n{name}\n{link}\nDescription: {description}")

            # Optional: Print rate limit info for debugging
            rate_limit_remaining = res.headers.get('X-RateLimit-Remaining')
            rate_limit_reset = res.headers.get('X-RateLimit-Reset')
            print(f"GitHub API Rate Limit Remaining: {rate_limit_remaining} (Resets at: {rate_limit_reset})")

        except requests.exceptions.RequestException as e:
            print(f"Error fetching GitHub data for keyword '{kw}': {e}")
            if e.response is not None:
                print(f"GitHub API Response Status: {e.response.status_code}")
                print(f"GitHub API Response Body: {e.response.text}")
            # Decide if you want to send a Telegram error message here or just log it.
            # send(f"🚨 Error searching for {kw}: {e}") # Uncomment if you want Telegram errors

    if not found_something:
        send("😴 No new repos found for today.")

# ... (if __name__ == "__main__": block remains the same) ...
