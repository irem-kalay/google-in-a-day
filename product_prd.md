# Product Requirements Document (PRD): Web Crawler & Real-Time Search Engine

## 1. Product Overview
The objective of this project is to build a functional, highly concurrent web crawler and a real-time search engine from scratch. As a System Architect, the goal is to direct AI agents to construct this system while demonstrating strict architectural sensibility, effective concurrency management, and human-in-the-loop verification.

## 2. Core Constraints & Guidelines
* **Native Focus:** The system must strictly utilize language-native standard libraries (e.g., Python's `urllib`, `html.parser`, `threading`, `queue`). Third-party high-level scraping or crawling libraries (such as Scrapy, Beautiful Soup, or Requests) are strictly prohibited.
* **AI Stewardship:** Code will be generated via AI agents (Cursor, Claude, etc.), but the architecture, thread safety, and logic must be actively validated and managed by the developer.

## 3. Technical Requirements: Indexer (The Crawler)
* **Recursive Crawling:** The crawler must initiate from a specified origin (seed) URL and crawl recursively up to a user-defined maximum depth (k).
* **Uniqueness Guarantee:** Implement a thread-safe "Visited" data structure (e.g., a Set protected by a Mutex) to ensure no single web page is processed or crawled more than once.
* **Back-Pressure Management:** The system must self-regulate its workload. It should monitor the depth of the URL queue and enforce a maximum rate of work or throttling to prevent memory overflow and network ban risks.

## 4. Technical Requirements: Searcher (The Query Engine)
* **Live Indexing:** The search functionality must be fully operational and accessible while the indexer is actively crawling and updating the database in the background.
* **Concurrency & Data Integrity:** Implement robust thread-safe data structures (e.g., Concurrent Maps, Mutexes/Locks) for the inverted index to prevent data corruption or race conditions during simultaneous read (search) and write (index) operations.
* **Query Output:** The search engine must return results as a list of triples: `(relevant_url, origin_url, depth)`.
* **Relevancy Heuristic:** Implement a baseline ranking algorithm to sort search results, such as term frequency (keyword count) or title matching.

## 5. System Visibility (UI/CLI Dashboard)
* **Real-Time Monitoring:** Provide a Command Line Interface (CLI) dashboard that continuously updates to display the current state of the system.
* **Metrics to Track:**
    * Indexing Progress: Number of URLs successfully processed vs. URLs currently in the queue.
    * Current Queue Depth.
    * System Status: Active back-pressure or throttling indicators.
* **Persistence (Bonus Feature):** Design the architecture so the crawling state can be saved and resumed after an unexpected interruption without starting from the origin URL again.

## 6. Success Criteria & Deliverables
The project will be evaluated on the following weighting:
* **Functionality (40%):** Accuracy of the crawl and successful concurrent searching.
* **Architectural Sensibility (40%):** Proper implementation of back-pressure and thread safety.
* **AI Stewardship (20%):** Ability to explain AI-generated code and justify architectural choices.

**Final Deliverables:**
* **Codebase:** Hosted on a public GitHub repository.
* **`readme.md`:** Instructions on how to run the system.
* **`product_prd.md`:** This requirements document.
* **`recommendation.md`:** A 2-paragraph production roadmap detailing how to deploy this system into a high-scale production environment.