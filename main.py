from collections import deque
import re
import sqlite3
import time
from urllib.parse import urlencode
from bs4 import BeautifulSoup
import requests

DB_PATH = "google_repos.db"


def parse_stars(star_text: str) -> int:
    """スター数の文字列（例: '1,234', '12.5k'）を整数に変換する"""
    text = star_text.strip().replace(",", "")
    if not text:
        return 0
    if text.lower().endswith("k"):
        return int(float(text[:-1]) * 1000)
    if text.lower().endswith("m"):
        return int(float(text[:-1]) * 1_000_000)
    try:
        return int(text)
    except ValueError:
        return 0


def init_db() -> tuple[sqlite3.Connection, dict[str, tuple[str, str, int]]]:
    """DBを初期化し、すでに保存済みのデータがあれば読み込む"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS repositories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            language TEXT,
            stars INTEGER
        )
    """)
    conn.commit()

    cursor.execute("SELECT name, language, stars FROM repositories")
    existing = {row[0]: (row[0], row[1], row[2]) for row in cursor.fetchall()}
    return conn, existing


def save_batch(conn: sqlite3.Connection, batch: list[tuple[str, str, int]]) -> None:
    """取得したページ分のデータを逐次DBへ保存する"""
    if not batch:
        return
    cursor = conn.cursor()
    cursor.executemany("""
        INSERT OR REPLACE INTO repositories (name, language, stars)
        VALUES (?, ?, ?)
    """, batch)
    conn.commit()


def request_with_retry(
    url: str, headers: dict[str, str], max_retries: int = 5
) -> requests.Response | None:
    """HTTP 429(レート制限)時は自動待機してリトライする"""
    for attempt in range(1, max_retries + 1):
        response = requests.get(url, headers=headers)
        # 課題要件: リクエストごとに必ず1秒待機
        time.sleep(1)

        if response.status_code == 200:
            return response

        if response.status_code in (429, 403):
            retry_after = response.headers.get("Retry-After")
            wait_sec = int(retry_after) if (retry_after and retry_after.isdigit()) else 30 * attempt
            print(f"アクセス制限(HTTP {response.status_code})を検知。{wait_sec}秒待機して再開します... ({attempt}/{max_retries})")
            time.sleep(wait_sec)
            continue

        print(f"ページ取得を終了します（ステータスコード: {response.status_code}）")
        return None

    return None


def fetch_pages(
    params_base: dict[str, str],
    repos_dict: dict[str, tuple[str, str, int]],
    conn: sqlite3.Connection,
    headers: dict[str, str],
) -> int:
    """指定パラメータでページを巡回し、到達した最終ページ番号を返す"""
    base_url = "https://github.com/orgs/google/repositories"
    repo_pattern = re.compile(r"^/google/[^/?#]+$")
    reserved_names = {
        "followers", "following", "repositories",
        "projects", "packages", "people", "sponsors",
    }

    label = urlencode(params_base) or "default"
    page = 1
    zero_new_streak = 0

    while True:
        params = {**params_base, "page": page}
        url = f"{base_url}?{urlencode(params)}"
        print(f"取得中 [{label} | p.{page}]: 現在の累計 {len(repos_dict)} 件")

        response = request_with_retry(url, headers)
        if response is None:
            break

        soup = BeautifulSoup(response.text, "html.parser")
        repo_links = soup.find_all("a", href=repo_pattern)

        if not repo_links:
            break

        page_batch: list[tuple[str, str, int]] = []
        new_in_page = 0

        for link in repo_links:
            href = link.get("href", "").split("?")[0].split("#")[0]
            repo_name = href.split("/")[-1].strip()
            if not repo_name or repo_name.lower() in reserved_names:
                continue

            # 1リポジトリ分のカード要素を特定
            card = link
            for parent in link.parents:
                if parent.name in ["li", "div", "article"]:
                    links_in_parent = {
                        a.get("href", "").split("?")[0]
                        for a in parent.find_all("a", href=repo_pattern)
                    }
                    if len(links_in_parent) == 1:
                        card = parent
                    else:
                        break

            # 主要な言語の取得
            lang_tag = card.select_one("span[itemprop='programmingLanguage']")
            if lang_tag:
                language = lang_tag.get_text(strip=True)
            else:
                color_dot = card.select_one("span.repo-language-color, span[style*='background-color']")
                if color_dot and color_dot.find_next_sibling():
                    language = color_dot.find_next_sibling().get_text(strip=True)
                else:
                    language = "未設定"

            # スター数の取得（リンクが無ければ 0 件として扱う）
            star_tag = card.select_one("a[href$='/stargazers']")
            stars = parse_stars(star_tag.get_text()) if star_tag else 0

            if repo_name not in repos_dict:
                new_in_page += 1
            record = (repo_name, language, stars)
            repos_dict[repo_name] = record
            page_batch.append(record)

        save_batch(conn, page_batch)

        # 3ページ連続で新規リポジトリが0件なら次の条件へスキップ
        if new_in_page == 0:
            zero_new_streak += 1
            if zero_new_streak >= 3:
                break
        else:
            zero_new_streak = 0

        # 次ページリンクの有無を確認
        next_link = (
            soup.select_one("a[rel='next']")
            or soup.select_one("a[aria-label='Next Page']")
            or soup.select_one("a.next_page")
            or soup.find("a", href=re.compile(rf"page={page + 1}\b"))
        )

        if not next_link:
            break

        page += 1

    return page


def scrape_and_show() -> None:
    """GitHubのGoogle組織リポジトリ一覧を全件スクレイピングしてDB保存・表示する"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    conn, repos_dict = init_db()
    print(f"開始時のDB保存件数: {len(repos_dict)} 件")

    # フェーズ1: ソート順・リポジトリ種別で大きく網羅する
    base_plans = [
        {},
        {"q": "sort:name-asc"},
        {"sort": "stargazers"},
        {"type": "archived"},
        {"type": "fork"},
    ]
    for plan in base_plans:
        fetch_pages(plan, repos_dict, conn, headers)

    # フェーズ2: アルファベット後半(g〜z)・数字・記号で残りを回収（40ページ上限時は2文字へ自動分割）
    initial_tokens = list("ghijklmnopqrstuvwxyz0123456789-_")
    expand_chars = list("aeioustrnlcdpmg-")
    queue = deque(initial_tokens)
    visited: set[str] = set()

    while queue:
        token = queue.popleft()
        if token in visited:
            continue
        visited.add(token)

        last_page = fetch_pages({"q": token}, repos_dict, conn, headers)

        if len(token) == 1 and last_page >= 40:
            for ch in expand_chars:
                next_token = token + ch
                if next_token not in visited:
                    queue.append(next_token)

    # 最終結果をSELECT文で表示
    cursor = conn.cursor()
    print("\n=== DBに保存されたリポジトリ一覧（SELECT結果） ===")
    cursor.execute("SELECT id, name, language, stars FROM repositories")
    rows = cursor.fetchall()

    for row in rows:
        print(f"ID: {row[0]} | リポジトリ名: {row[1]} | 主要な言語: {row[2]} | スター数: {row[3]}")

    print(f"\n合計保存件数: {len(rows)} 件")
    conn.close()


if __name__ == "__main__":
    scrape_and_show()