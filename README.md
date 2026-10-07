#### Pickleball Courts NYC

Pickle is a chat-based planner for New Yorkers who want to squeeze in a game of pickleball after work. Tell it where you are, and it finds the three closest courts by actual travel distance, then tells you how much daylight you have left to play. It's built with FastAPI, LiteLLM and Gemini, and the page shows each tool call the agent makes above its answer.

#### Tools
find_pickleball_courts: Finds the 3 nearest court sites in NYC, ranked by street travel distance (walking, biking or driving), with lights, surface, access, reservation info and fees when known. It also returns current local time and sunset.
get_sun_times: Returns the current local time, today's sunrise and sunset, tomorrow's sunset, and whether it is dark right now.
get_distance_to_court: Returns the distance (km and miles) and travel time between you and a specific court.
can_i_play_before_dark: Calculates travel time to a court and how many minutes of daylight remain when you arrive.

Data comes from OpenStreetMap (Nominatim, Overpass), Open-Meteo and the OSM routing server. None of them need an API key.

#### Setup
Create a GCP project with billing and the Agent Platform API enabled (older docs and the endpoint itself still call it Vertex AI).
Run gcloud auth application-default login. The app uses your gcloud default project.
Run uv run app.py, then open http://localhost:8000.
How to use it

#### Example Queries
Type a New York neighborhood, park or address into the chat box. Mention how you're getting there (walking, biking or driving) if you want courts ranked for that mode; otherwise it assumes walking. Pickle is limited to New York City.

Try:

"I'm in Astoria, where can I play pickleball?"
"Can I bike from Brooklyn Bridge Park to a court before dark?"
"How far is the nearest court from 350 5th Ave, New York?"
Notes
OpenStreetMap coverage is incomplete, so some courts may be missing or have unknown lighting and reservation details.
Travel times marked as approximate come from a straight-line estimate used when the routing server is unavailable.


A title

A short description of who the project is for and what it does

A list of your tools, with one line on what each does

Explain how to use it. If it's a free-text input (like a chatbot), 3 example queries
