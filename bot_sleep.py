"""BotSleep - Automatic Minecraft Sleep Agent

Detects nightfall / thunderstorms, locates the nearest bed, moves to it,
and sleeps to skip the night.
"""

import time
from client import MinecraftAgentClient


def main():
    print("=== Starting BotSleep Agent ===")
    client = MinecraftAgentClient(
        base_url="http://127.0.0.1:18900/api/v1",
        username="SleepBot",
        password="agent-secret-pass",
    )

    try:
        print("Connecting to mc-agent-api and Minecraft server...")
        status = client.login()
        print(f"Logged in as {client.username} at {status['pos']} in dimension '{status['dimension']}'")
        client.chat("SleepBot is online and watching the sky!")

        bed_pos = None

        while True:
            st = client.get_status().get("result", {})
            is_day = st.get("is_day", True)
            is_sleeping = st.get("is_sleeping", False)
            thundering = st.get("thundering", False)
            time_of_day = st.get("time_of_day", 0)

            print(f"[Time: {time_of_day}] is_day={is_day}, thundering={thundering}, is_sleeping={is_sleeping}")

            if not is_sleeping and (not is_day or thundering):
                print("Night or storm detected! Searching for a bed...")
                bed = client.find_nearest_block(name_contains="_bed", radius=16)
                if bed:
                    bed_pos = bed["pos"]
                    print(f"Found bed at {bed_pos} ({bed['name']}), distance: {bed['dist']} blocks")
                    client.chat(f"Going to sleep in {bed['name']} at {bed_pos}...")
                    res = client.sleep_in_bed(bed_pos)
                    print("Sleep response:", res)
                    if res.get("result", {}).get("slept"):
                        client.chat("Zzz... Good night!")
                else:
                    print("No bed found within 16 blocks.")
            elif is_sleeping and is_day:
                print("Morning has arrived! Waking up...")
                if bed_pos:
                    client.wake(bed_pos)
                    client.chat("Good morning everyone!")

            time.sleep(5)

    except KeyboardInterrupt:
        print("\nStopping BotSleep...")
        client.chat("SleepBot going offline. Bye!")
        client.logout()


if __name__ == "__main__":
    main()
