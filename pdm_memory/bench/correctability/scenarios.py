"""
Correctability Benchmark — Scenario Dataset
============================================

100 verifiable factual scenarios seeded across 4 domains:
  - science    (25 scenarios)
  - geography  (25 scenarios)
  - history    (25 scenarios)
  - tech       (25 scenarios)

Each scenario has:
  - A WRONG answer seeded at high authority  (p_magnitude=85, "Signature A")
  - A CORRECT answer seeded at low authority (p_magnitude=40, "Signature B")

All answers are publicly verifiable facts with a single, unambiguous
ground truth — no opinion, no ambiguity.

Credit: scenario structure inspired by Bjoern Janssen's correctability probe
for adaptive memory systems.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Scenario dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchScenario:
    """
    One correctability test scenario.

    Attributes:
        id:               Unique scenario identifier (stable across runs).
        domain:           One of: science, geography, history, tech.
        query:            The question the harness asks the memory system.
        wrong_answer:     The WRONG text — seeded as Signature A at P=85.
        correct_answer:   The CORRECT text — seeded as Signature B at P=40.
        tags_wrong:       Intent tags for the wrong signature.
        tags_correct:     Intent tags for the correct signature.
        drawer:           Drawer name both signatures share (ensures competition).
    """

    id: str
    domain: str
    query: str
    wrong_answer: str
    correct_answer: str
    tags_wrong: list[str] = field(default_factory=list)
    tags_correct: list[str] = field(default_factory=list)
    drawer: str = "correctability"


# ---------------------------------------------------------------------------
# Initial pressure levels — spec-defined
# ---------------------------------------------------------------------------

P_WRONG: float = 85.0    # Signature A — wrong but authoritative
P_CORRECT: float = 40.0  # Signature B — correct but low authority


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

_SCENARIOS_RAW: list[dict] = [
    # ------------------------------------------------------------------ science
    {
        "id": "sci_001", "domain": "science",
        "query": "At what temperature does water boil at sea level?",
        "wrong": "Water boils at 90 degrees Celsius at sea level.",
        "correct": "Water boils at 100 degrees Celsius at sea level.",
        "tags": ["science", "water", "temperature", "boiling"],
    },
    {
        "id": "sci_002", "domain": "science",
        "query": "What is the chemical symbol for gold?",
        "wrong": "The chemical symbol for gold is Go.",
        "correct": "The chemical symbol for gold is Au.",
        "tags": ["science", "chemistry", "gold", "symbol"],
    },
    {
        "id": "sci_003", "domain": "science",
        "query": "How many chromosomes do humans have?",
        "wrong": "Humans have 48 chromosomes in each somatic cell.",
        "correct": "Humans have 46 chromosomes in each somatic cell.",
        "tags": ["science", "biology", "chromosomes", "human"],
    },
    {
        "id": "sci_004", "domain": "science",
        "query": "What is the speed of light in a vacuum?",
        "wrong": "The speed of light in a vacuum is approximately 200,000 km/s.",
        "correct": "The speed of light in a vacuum is approximately 300,000 km/s.",
        "tags": ["science", "physics", "light", "speed"],
    },
    {
        "id": "sci_005", "domain": "science",
        "query": "What planet is closest to the Sun?",
        "wrong": "Venus is the closest planet to the Sun.",
        "correct": "Mercury is the closest planet to the Sun.",
        "tags": ["science", "astronomy", "planet", "sun"],
    },
    {
        "id": "sci_006", "domain": "science",
        "query": "What is the atomic number of carbon?",
        "wrong": "The atomic number of carbon is 8.",
        "correct": "The atomic number of carbon is 6.",
        "tags": ["science", "chemistry", "carbon", "atomic_number"],
    },
    {
        "id": "sci_007", "domain": "science",
        "query": "What gas do plants absorb during photosynthesis?",
        "wrong": "Plants absorb oxygen during photosynthesis.",
        "correct": "Plants absorb carbon dioxide during photosynthesis.",
        "tags": ["science", "biology", "photosynthesis", "plant"],
    },
    {
        "id": "sci_008", "domain": "science",
        "query": "How many bones are in the adult human body?",
        "wrong": "The adult human body has 300 bones.",
        "correct": "The adult human body has 206 bones.",
        "tags": ["science", "anatomy", "bones", "human"],
    },
    {
        "id": "sci_009", "domain": "science",
        "query": "What is the powerhouse of the cell?",
        "wrong": "The nucleus is the powerhouse of the cell.",
        "correct": "The mitochondrion is the powerhouse of the cell.",
        "tags": ["science", "biology", "cell", "organelle"],
    },
    {
        "id": "sci_010", "domain": "science",
        "query": "What is the SI unit of electric current?",
        "wrong": "The SI unit of electric current is the Volt.",
        "correct": "The SI unit of electric current is the Ampere.",
        "tags": ["science", "physics", "electricity", "unit"],
    },
    {
        "id": "sci_011", "domain": "science",
        "query": "What is Newton's second law of motion?",
        "wrong": "Newton's second law states that every action has an equal and opposite reaction.",
        "correct": "Newton's second law states that force equals mass times acceleration (F=ma).",
        "tags": ["science", "physics", "newton", "law"],
    },
    {
        "id": "sci_012", "domain": "science",
        "query": "What is the most abundant gas in Earth's atmosphere?",
        "wrong": "Oxygen is the most abundant gas in Earth's atmosphere.",
        "correct": "Nitrogen is the most abundant gas in Earth's atmosphere.",
        "tags": ["science", "atmosphere", "gas", "nitrogen"],
    },
    {
        "id": "sci_013", "domain": "science",
        "query": "What is the chemical formula for water?",
        "wrong": "The chemical formula for water is HO.",
        "correct": "The chemical formula for water is H2O.",
        "tags": ["science", "chemistry", "water", "formula"],
    },
    {
        "id": "sci_014", "domain": "science",
        "query": "How many planets are in our solar system?",
        "wrong": "There are 9 planets in our solar system.",
        "correct": "There are 8 planets in our solar system.",
        "tags": ["science", "astronomy", "planets", "solar_system"],
    },
    {
        "id": "sci_015", "domain": "science",
        "query": "What is the hardest natural mineral?",
        "wrong": "Quartz is the hardest natural mineral.",
        "correct": "Diamond is the hardest natural mineral.",
        "tags": ["science", "geology", "mineral", "hardness"],
    },
    {
        "id": "sci_016", "domain": "science",
        "query": "What is the pH of pure water at 25°C?",
        "wrong": "The pH of pure water at 25°C is 6.",
        "correct": "The pH of pure water at 25°C is 7.",
        "tags": ["science", "chemistry", "pH", "water"],
    },
    {
        "id": "sci_017", "domain": "science",
        "query": "What element has the symbol Fe?",
        "wrong": "The element with symbol Fe is Fluorine.",
        "correct": "The element with symbol Fe is Iron.",
        "tags": ["science", "chemistry", "element", "symbol"],
    },
    {
        "id": "sci_018", "domain": "science",
        "query": "How long does it take light to travel from the Sun to Earth?",
        "wrong": "It takes about 2 seconds for light to travel from the Sun to Earth.",
        "correct": "It takes about 8 minutes for light to travel from the Sun to Earth.",
        "tags": ["science", "astronomy", "light", "sun"],
    },
    {
        "id": "sci_019", "domain": "science",
        "query": "What is the freezing point of water in Celsius?",
        "wrong": "Water freezes at -10 degrees Celsius.",
        "correct": "Water freezes at 0 degrees Celsius.",
        "tags": ["science", "physics", "water", "freezing"],
    },
    {
        "id": "sci_020", "domain": "science",
        "query": "Which planet is known as the Red Planet?",
        "wrong": "Jupiter is known as the Red Planet.",
        "correct": "Mars is known as the Red Planet.",
        "tags": ["science", "astronomy", "planet", "mars"],
    },
    {
        "id": "sci_021", "domain": "science",
        "query": "What is the largest organ in the human body?",
        "wrong": "The liver is the largest organ in the human body.",
        "correct": "The skin is the largest organ in the human body.",
        "tags": ["science", "anatomy", "organ", "human"],
    },
    {
        "id": "sci_022", "domain": "science",
        "query": "What force keeps planets in orbit around the Sun?",
        "wrong": "Magnetism keeps planets in orbit around the Sun.",
        "correct": "Gravity keeps planets in orbit around the Sun.",
        "tags": ["science", "physics", "gravity", "orbit"],
    },
    {
        "id": "sci_023", "domain": "science",
        "query": "What is the unit of frequency?",
        "wrong": "The unit of frequency is the Newton.",
        "correct": "The unit of frequency is the Hertz.",
        "tags": ["science", "physics", "frequency", "unit"],
    },
    {
        "id": "sci_024", "domain": "science",
        "query": "What is the chemical formula for table salt?",
        "wrong": "The chemical formula for table salt is CaCl2.",
        "correct": "The chemical formula for table salt is NaCl.",
        "tags": ["science", "chemistry", "salt", "formula"],
    },
    {
        "id": "sci_025", "domain": "science",
        "query": "How many moons does Mars have?",
        "wrong": "Mars has one moon.",
        "correct": "Mars has two moons: Phobos and Deimos.",
        "tags": ["science", "astronomy", "mars", "moon"],
    },

    # --------------------------------------------------------------- geography
    {
        "id": "geo_001", "domain": "geography",
        "query": "What is the capital of Australia?",
        "wrong": "Sydney is the capital of Australia.",
        "correct": "Canberra is the capital of Australia.",
        "tags": ["geography", "australia", "capital", "city"],
    },
    {
        "id": "geo_002", "domain": "geography",
        "query": "What is the largest ocean on Earth?",
        "wrong": "The Atlantic Ocean is the largest ocean on Earth.",
        "correct": "The Pacific Ocean is the largest ocean on Earth.",
        "tags": ["geography", "ocean", "size", "earth"],
    },
    {
        "id": "geo_003", "domain": "geography",
        "query": "What is the longest river in the world?",
        "wrong": "The Amazon is the longest river in the world.",
        "correct": "The Nile is the longest river in the world.",
        "tags": ["geography", "river", "longest", "world"],
    },
    {
        "id": "geo_004", "domain": "geography",
        "query": "What is the capital of Canada?",
        "wrong": "Toronto is the capital of Canada.",
        "correct": "Ottawa is the capital of Canada.",
        "tags": ["geography", "canada", "capital", "city"],
    },
    {
        "id": "geo_005", "domain": "geography",
        "query": "Which continent is Egypt located on?",
        "wrong": "Egypt is located in Asia.",
        "correct": "Egypt is located in Africa.",
        "tags": ["geography", "egypt", "continent", "africa"],
    },
    {
        "id": "geo_006", "domain": "geography",
        "query": "What is the smallest country in the world by area?",
        "wrong": "Monaco is the smallest country in the world by area.",
        "correct": "Vatican City is the smallest country in the world by area.",
        "tags": ["geography", "country", "smallest", "area"],
    },
    {
        "id": "geo_007", "domain": "geography",
        "query": "What is the highest mountain in the world?",
        "wrong": "K2 is the highest mountain in the world.",
        "correct": "Mount Everest is the highest mountain in the world.",
        "tags": ["geography", "mountain", "highest", "everest"],
    },
    {
        "id": "geo_008", "domain": "geography",
        "query": "What is the capital of Brazil?",
        "wrong": "Rio de Janeiro is the capital of Brazil.",
        "correct": "Brasília is the capital of Brazil.",
        "tags": ["geography", "brazil", "capital", "city"],
    },
    {
        "id": "geo_009", "domain": "geography",
        "query": "Which country has the largest population?",
        "wrong": "India has the largest population in the world.",
        "correct": "China has the largest population in the world.",
        "tags": ["geography", "population", "china", "country"],
    },
    {
        "id": "geo_010", "domain": "geography",
        "query": "What is the largest country by land area?",
        "wrong": "Canada is the largest country by land area.",
        "correct": "Russia is the largest country by land area.",
        "tags": ["geography", "country", "area", "russia"],
    },
    {
        "id": "geo_011", "domain": "geography",
        "query": "What is the capital of Japan?",
        "wrong": "Osaka is the capital of Japan.",
        "correct": "Tokyo is the capital of Japan.",
        "tags": ["geography", "japan", "capital", "city"],
    },
    {
        "id": "geo_012", "domain": "geography",
        "query": "On which continent is the Amazon rainforest located?",
        "wrong": "The Amazon rainforest is located in Africa.",
        "correct": "The Amazon rainforest is located in South America.",
        "tags": ["geography", "amazon", "rainforest", "continent"],
    },
    {
        "id": "geo_013", "domain": "geography",
        "query": "What is the capital of South Africa?",
        "wrong": "Johannesburg is the capital of South Africa.",
        "correct": "Pretoria is the administrative capital of South Africa.",
        "tags": ["geography", "south_africa", "capital", "city"],
    },
    {
        "id": "geo_014", "domain": "geography",
        "query": "What is the largest desert in the world?",
        "wrong": "The Sahara is the largest desert in the world.",
        "correct": "Antarctica is the largest desert in the world.",
        "tags": ["geography", "desert", "largest", "world"],
    },
    {
        "id": "geo_015", "domain": "geography",
        "query": "What is the capital of Germany?",
        "wrong": "Munich is the capital of Germany.",
        "correct": "Berlin is the capital of Germany.",
        "tags": ["geography", "germany", "capital", "city"],
    },
    {
        "id": "geo_016", "domain": "geography",
        "query": "How many continents are there?",
        "wrong": "There are 6 continents.",
        "correct": "There are 7 continents.",
        "tags": ["geography", "continents", "count", "world"],
    },
    {
        "id": "geo_017", "domain": "geography",
        "query": "What ocean lies between Europe and North America?",
        "wrong": "The Pacific Ocean lies between Europe and North America.",
        "correct": "The Atlantic Ocean lies between Europe and North America.",
        "tags": ["geography", "ocean", "atlantic", "europe"],
    },
    {
        "id": "geo_018", "domain": "geography",
        "query": "What is the capital of Argentina?",
        "wrong": "Santiago is the capital of Argentina.",
        "correct": "Buenos Aires is the capital of Argentina.",
        "tags": ["geography", "argentina", "capital", "city"],
    },
    {
        "id": "geo_019", "domain": "geography",
        "query": "In which country is the Sahara desert located?",
        "wrong": "The Sahara desert is entirely within Egypt.",
        "correct": "The Sahara desert spans multiple North African countries.",
        "tags": ["geography", "sahara", "desert", "africa"],
    },
    {
        "id": "geo_020", "domain": "geography",
        "query": "What is the capital of India?",
        "wrong": "Mumbai is the capital of India.",
        "correct": "New Delhi is the capital of India.",
        "tags": ["geography", "india", "capital", "city"],
    },
    {
        "id": "geo_021", "domain": "geography",
        "query": "Which country is the Nile River primarily associated with?",
        "wrong": "The Nile River is primarily associated with Sudan.",
        "correct": "The Nile River is primarily associated with Egypt.",
        "tags": ["geography", "nile", "river", "egypt"],
    },
    {
        "id": "geo_022", "domain": "geography",
        "query": "What is the largest lake in the world by surface area?",
        "wrong": "Lake Superior is the largest lake in the world by surface area.",
        "correct": "The Caspian Sea is the largest lake in the world by surface area.",
        "tags": ["geography", "lake", "caspian", "size"],
    },
    {
        "id": "geo_023", "domain": "geography",
        "query": "What is the capital of Egypt?",
        "wrong": "Alexandria is the capital of Egypt.",
        "correct": "Cairo is the capital of Egypt.",
        "tags": ["geography", "egypt", "capital", "city"],
    },
    {
        "id": "geo_024", "domain": "geography",
        "query": "On which continent is the country of Nigeria located?",
        "wrong": "Nigeria is located in Asia.",
        "correct": "Nigeria is located in Africa.",
        "tags": ["geography", "nigeria", "continent", "africa"],
    },
    {
        "id": "geo_025", "domain": "geography",
        "query": "What is the capital of South Korea?",
        "wrong": "Busan is the capital of South Korea.",
        "correct": "Seoul is the capital of South Korea.",
        "tags": ["geography", "south_korea", "capital", "city"],
    },

    # ----------------------------------------------------------------- history
    {
        "id": "his_001", "domain": "history",
        "query": "In what year did World War II begin?",
        "wrong": "World War II began in 1940.",
        "correct": "World War II began in 1939.",
        "tags": ["history", "world_war", "ww2", "year"],
    },
    {
        "id": "his_002", "domain": "history",
        "query": "Who was the first President of the United States?",
        "wrong": "John Adams was the first President of the United States.",
        "correct": "George Washington was the first President of the United States.",
        "tags": ["history", "usa", "president", "first"],
    },
    {
        "id": "his_003", "domain": "history",
        "query": "In what year did World War I begin?",
        "wrong": "World War I began in 1916.",
        "correct": "World War I began in 1914.",
        "tags": ["history", "world_war", "ww1", "year"],
    },
    {
        "id": "his_004", "domain": "history",
        "query": "Who invented the telephone?",
        "wrong": "Thomas Edison invented the telephone.",
        "correct": "Alexander Graham Bell invented the telephone.",
        "tags": ["history", "invention", "telephone", "bell"],
    },
    {
        "id": "his_005", "domain": "history",
        "query": "In what year did the Berlin Wall fall?",
        "wrong": "The Berlin Wall fell in 1991.",
        "correct": "The Berlin Wall fell in 1989.",
        "tags": ["history", "berlin_wall", "germany", "year"],
    },
    {
        "id": "his_006", "domain": "history",
        "query": "Who was the first person to walk on the Moon?",
        "wrong": "Buzz Aldrin was the first person to walk on the Moon.",
        "correct": "Neil Armstrong was the first person to walk on the Moon.",
        "tags": ["history", "moon", "nasa", "astronaut"],
    },
    {
        "id": "his_007", "domain": "history",
        "query": "In what year did the French Revolution begin?",
        "wrong": "The French Revolution began in 1800.",
        "correct": "The French Revolution began in 1789.",
        "tags": ["history", "france", "revolution", "year"],
    },
    {
        "id": "his_008", "domain": "history",
        "query": "Who was the first woman to win a Nobel Prize?",
        "wrong": "Rosalind Franklin was the first woman to win a Nobel Prize.",
        "correct": "Marie Curie was the first woman to win a Nobel Prize.",
        "tags": ["history", "nobel", "woman", "curie"],
    },
    {
        "id": "his_009", "domain": "history",
        "query": "In what year was the United States Declaration of Independence signed?",
        "wrong": "The US Declaration of Independence was signed in 1782.",
        "correct": "The US Declaration of Independence was signed in 1776.",
        "tags": ["history", "usa", "declaration", "independence"],
    },
    {
        "id": "his_010", "domain": "history",
        "query": "Who developed the theory of general relativity?",
        "wrong": "Isaac Newton developed the theory of general relativity.",
        "correct": "Albert Einstein developed the theory of general relativity.",
        "tags": ["history", "science", "relativity", "einstein"],
    },
    {
        "id": "his_011", "domain": "history",
        "query": "In what year did the Soviet Union dissolve?",
        "wrong": "The Soviet Union dissolved in 1989.",
        "correct": "The Soviet Union dissolved in 1991.",
        "tags": ["history", "soviet_union", "ussr", "year"],
    },
    {
        "id": "his_012", "domain": "history",
        "query": "Who wrote the play 'Hamlet'?",
        "wrong": "Christopher Marlowe wrote the play Hamlet.",
        "correct": "William Shakespeare wrote the play Hamlet.",
        "tags": ["history", "literature", "hamlet", "shakespeare"],
    },
    {
        "id": "his_013", "domain": "history",
        "query": "In what year did Christopher Columbus reach the Americas?",
        "wrong": "Christopher Columbus reached the Americas in 1498.",
        "correct": "Christopher Columbus reached the Americas in 1492.",
        "tags": ["history", "columbus", "americas", "year"],
    },
    {
        "id": "his_014", "domain": "history",
        "query": "Which empire was Julius Caesar a leader of?",
        "wrong": "Julius Caesar was a leader of the Greek Empire.",
        "correct": "Julius Caesar was a leader of the Roman Republic and Empire.",
        "tags": ["history", "caesar", "roman", "empire"],
    },
    {
        "id": "his_015", "domain": "history",
        "query": "In what year did the Titanic sink?",
        "wrong": "The Titanic sank in 1915.",
        "correct": "The Titanic sank in 1912.",
        "tags": ["history", "titanic", "ship", "year"],
    },
    {
        "id": "his_016", "domain": "history",
        "query": "Who painted the Mona Lisa?",
        "wrong": "Michelangelo painted the Mona Lisa.",
        "correct": "Leonardo da Vinci painted the Mona Lisa.",
        "tags": ["history", "art", "mona_lisa", "leonardo"],
    },
    {
        "id": "his_017", "domain": "history",
        "query": "In what year did World War II end?",
        "wrong": "World War II ended in 1944.",
        "correct": "World War II ended in 1945.",
        "tags": ["history", "world_war", "ww2", "end"],
    },
    {
        "id": "his_018", "domain": "history",
        "query": "Who was the first Emperor of China?",
        "wrong": "Confucius was the first Emperor of China.",
        "correct": "Qin Shi Huang was the first Emperor of China.",
        "tags": ["history", "china", "emperor", "qin"],
    },
    {
        "id": "his_019", "domain": "history",
        "query": "In what year did the first moon landing occur?",
        "wrong": "The first moon landing occurred in 1971.",
        "correct": "The first moon landing occurred in 1969.",
        "tags": ["history", "moon", "landing", "nasa"],
    },
    {
        "id": "his_020", "domain": "history",
        "query": "Who invented the printing press?",
        "wrong": "Leonardo da Vinci invented the printing press.",
        "correct": "Johannes Gutenberg invented the printing press.",
        "tags": ["history", "invention", "printing_press", "gutenberg"],
    },
    {
        "id": "his_021", "domain": "history",
        "query": "In what year was the Magna Carta signed?",
        "wrong": "The Magna Carta was signed in 1250.",
        "correct": "The Magna Carta was signed in 1215.",
        "tags": ["history", "magna_carta", "england", "year"],
    },
    {
        "id": "his_022", "domain": "history",
        "query": "Who was the leader of Nazi Germany during World War II?",
        "wrong": "Joseph Stalin was the leader of Nazi Germany during World War II.",
        "correct": "Adolf Hitler was the leader of Nazi Germany during World War II.",
        "tags": ["history", "ww2", "germany", "nazi"],
    },
    {
        "id": "his_023", "domain": "history",
        "query": "In what year was the Eiffel Tower built?",
        "wrong": "The Eiffel Tower was built in 1900.",
        "correct": "The Eiffel Tower was completed in 1889.",
        "tags": ["history", "eiffel_tower", "france", "year"],
    },
    {
        "id": "his_024", "domain": "history",
        "query": "Who invented dynamite?",
        "wrong": "Thomas Edison invented dynamite.",
        "correct": "Alfred Nobel invented dynamite.",
        "tags": ["history", "invention", "dynamite", "nobel"],
    },
    {
        "id": "his_025", "domain": "history",
        "query": "In what year was the Great Wall of China construction begun?",
        "wrong": "The Great Wall of China was begun in the 1st century AD.",
        "correct": "The Great Wall of China was begun in the 7th century BC.",
        "tags": ["history", "china", "great_wall", "construction"],
    },

    # --------------------------------------------------------------------- tech
    {
        "id": "tec_001", "domain": "tech",
        "query": "What is the time complexity of appending to a Python list?",
        "wrong": "Appending to a Python list has O(n) time complexity.",
        "correct": "Appending to a Python list has O(1) amortised time complexity.",
        "tags": ["tech", "python", "list", "complexity"],
    },
    {
        "id": "tec_002", "domain": "tech",
        "query": "What does CPU stand for?",
        "wrong": "CPU stands for Central Processing Unit of memory.",
        "correct": "CPU stands for Central Processing Unit.",
        "tags": ["tech", "hardware", "cpu", "acronym"],
    },
    {
        "id": "tec_003", "domain": "tech",
        "query": "What does HTTP stand for?",
        "wrong": "HTTP stands for HyperText Terminal Protocol.",
        "correct": "HTTP stands for HyperText Transfer Protocol.",
        "tags": ["tech", "networking", "http", "acronym"],
    },
    {
        "id": "tec_004", "domain": "tech",
        "query": "Which programming language is primarily used for iOS development?",
        "wrong": "Java is primarily used for iOS development.",
        "correct": "Swift is primarily used for iOS development.",
        "tags": ["tech", "ios", "swift", "mobile"],
    },
    {
        "id": "tec_005", "domain": "tech",
        "query": "What does SQL stand for?",
        "wrong": "SQL stands for Simple Query Language.",
        "correct": "SQL stands for Structured Query Language.",
        "tags": ["tech", "database", "sql", "acronym"],
    },
    {
        "id": "tec_006", "domain": "tech",
        "query": "What is the default port for HTTPS?",
        "wrong": "The default port for HTTPS is 8080.",
        "correct": "The default port for HTTPS is 443.",
        "tags": ["tech", "networking", "https", "port"],
    },
    {
        "id": "tec_007", "domain": "tech",
        "query": "What does RAM stand for?",
        "wrong": "RAM stands for Read And Modify memory.",
        "correct": "RAM stands for Random Access Memory.",
        "tags": ["tech", "hardware", "ram", "acronym"],
    },
    {
        "id": "tec_008", "domain": "tech",
        "query": "What is the time complexity of binary search?",
        "wrong": "Binary search has O(n) time complexity.",
        "correct": "Binary search has O(log n) time complexity.",
        "tags": ["tech", "algorithm", "binary_search", "complexity"],
    },
    {
        "id": "tec_009", "domain": "tech",
        "query": "Which company developed the Python programming language?",
        "wrong": "Microsoft developed the Python programming language.",
        "correct": "Python was created by Guido van Rossum; no company owns it — it is community-led.",
        "tags": ["tech", "python", "history", "creator"],
    },
    {
        "id": "tec_010", "domain": "tech",
        "query": "What does API stand for?",
        "wrong": "API stands for Application Process Interface.",
        "correct": "API stands for Application Programming Interface.",
        "tags": ["tech", "software", "api", "acronym"],
    },
    {
        "id": "tec_011", "domain": "tech",
        "query": "What does DNS stand for?",
        "wrong": "DNS stands for Data Network System.",
        "correct": "DNS stands for Domain Name System.",
        "tags": ["tech", "networking", "dns", "acronym"],
    },
    {
        "id": "tec_012", "domain": "tech",
        "query": "What is the default port for HTTP?",
        "wrong": "The default port for HTTP is 8080.",
        "correct": "The default port for HTTP is 80.",
        "tags": ["tech", "networking", "http", "port"],
    },
    {
        "id": "tec_013", "domain": "tech",
        "query": "In Git, what command is used to create a new branch?",
        "wrong": "In Git, `git new branch_name` creates a new branch.",
        "correct": "In Git, `git branch branch_name` or `git checkout -b branch_name` creates a new branch.",
        "tags": ["tech", "git", "branch", "command"],
    },
    {
        "id": "tec_014", "domain": "tech",
        "query": "What does JSON stand for?",
        "wrong": "JSON stands for JavaScript Object Node.",
        "correct": "JSON stands for JavaScript Object Notation.",
        "tags": ["tech", "data", "json", "acronym"],
    },
    {
        "id": "tec_015", "domain": "tech",
        "query": "What is the time complexity of accessing an element in a hash table?",
        "wrong": "Accessing an element in a hash table has O(log n) average time complexity.",
        "correct": "Accessing an element in a hash table has O(1) average time complexity.",
        "tags": ["tech", "algorithm", "hash_table", "complexity"],
    },
    {
        "id": "tec_016", "domain": "tech",
        "query": "What does OOP stand for?",
        "wrong": "OOP stands for Ordered Object Programming.",
        "correct": "OOP stands for Object-Oriented Programming.",
        "tags": ["tech", "software", "oop", "acronym"],
    },
    {
        "id": "tec_017", "domain": "tech",
        "query": "What language is primarily used to style web pages?",
        "wrong": "JavaScript is primarily used to style web pages.",
        "correct": "CSS (Cascading Style Sheets) is primarily used to style web pages.",
        "tags": ["tech", "web", "css", "styling"],
    },
    {
        "id": "tec_018", "domain": "tech",
        "query": "What does URL stand for?",
        "wrong": "URL stands for Universal Resource Locator.",
        "correct": "URL stands for Uniform Resource Locator.",
        "tags": ["tech", "networking", "url", "acronym"],
    },
    {
        "id": "tec_019", "domain": "tech",
        "query": "What is the largest unit of digital storage listed here: KB, MB, GB, TB?",
        "wrong": "GB is the largest among KB, MB, GB, TB.",
        "correct": "TB (Terabyte) is the largest among KB, MB, GB, TB.",
        "tags": ["tech", "storage", "units", "terabyte"],
    },
    {
        "id": "tec_020", "domain": "tech",
        "query": "What does IDE stand for in software development?",
        "wrong": "IDE stands for Integrated Debugging Environment.",
        "correct": "IDE stands for Integrated Development Environment.",
        "tags": ["tech", "software", "ide", "acronym"],
    },
    {
        "id": "tec_021", "domain": "tech",
        "query": "Which data structure uses LIFO ordering?",
        "wrong": "A queue uses LIFO (Last-In, First-Out) ordering.",
        "correct": "A stack uses LIFO (Last-In, First-Out) ordering.",
        "tags": ["tech", "algorithm", "stack", "lifo"],
    },
    {
        "id": "tec_022", "domain": "tech",
        "query": "What does the `git commit` command do?",
        "wrong": "`git commit` pushes local changes to a remote repository.",
        "correct": "`git commit` records staged changes in the local repository history.",
        "tags": ["tech", "git", "commit", "command"],
    },
    {
        "id": "tec_023", "domain": "tech",
        "query": "What does IP stand for in networking?",
        "wrong": "IP stands for Internet Process.",
        "correct": "IP stands for Internet Protocol.",
        "tags": ["tech", "networking", "ip", "acronym"],
    },
    {
        "id": "tec_024", "domain": "tech",
        "query": "What language is primarily used for Android development?",
        "wrong": "Swift is primarily used for Android development.",
        "correct": "Kotlin (and Java) are primarily used for Android development.",
        "tags": ["tech", "android", "kotlin", "mobile"],
    },
    {
        "id": "tec_025", "domain": "tech",
        "query": "What does GPU stand for?",
        "wrong": "GPU stands for General Processing Utility.",
        "correct": "GPU stands for Graphics Processing Unit.",
        "tags": ["tech", "hardware", "gpu", "acronym"],
    },
]


# ---------------------------------------------------------------------------
# Build the scenario list
# ---------------------------------------------------------------------------


def _build(raw: dict) -> BenchScenario:
    tags = raw["tags"]
    return BenchScenario(
        id=raw["id"],
        domain=raw["domain"],
        query=raw["query"],
        wrong_answer=raw["wrong"],
        correct_answer=raw["correct"],
        tags_wrong=tags,
        tags_correct=tags,
        drawer="correctability",
    )


#: Full list of 100 benchmark scenarios — one module-level constant.
ALL_SCENARIOS: list[BenchScenario] = [_build(r) for r in _SCENARIOS_RAW]

#: Scenarios grouped by domain for easy filtering.
SCENARIOS_BY_DOMAIN: dict = {
    domain: [s for s in ALL_SCENARIOS if s.domain == domain]
    for domain in ("science", "geography", "history", "tech")
}


def get_scenarios(domains: list[str] | None = None) -> list[BenchScenario]:
    """
    Return scenarios, optionally filtered to specific domains.

    Args:
        domains: List of domain names to include.  If None, all 4 domains
                 are included.

    Returns:
        List[BenchScenario], minimum 100 when no filter applied.
    """
    if domains is None:
        return list(ALL_SCENARIOS)
    return [s for s in ALL_SCENARIOS if s.domain in domains]
