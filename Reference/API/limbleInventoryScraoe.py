"""Compatibility script for Limble inventory scraping experiments."""

from integrations.limble import LimbleClient


def main() -> None:
    print(LimbleClient().list_assets())


if __name__ == "__main__":
    main()
