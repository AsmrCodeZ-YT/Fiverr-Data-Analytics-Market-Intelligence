#!/usr/bin/env python3
import sys

import fiverr_gigs_scraper


if __name__ == "__main__":
    fiverr_gigs_scraper.main(["--reviews-only", *sys.argv[1:]])
