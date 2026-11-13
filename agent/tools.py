"""OpenAI Function Calling Tool Definitions for Minecraft Procedural Skills."""

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "pillar_up",
            "description": "Jump and place solid blocks underneath to tower up vertically. Useful to evade ground monsters (zombies, creepers, spiders).",
            "parameters": {
                "type": "object",
                "properties": {
                    "height": {
                        "type": "integer",
                        "description": "Number of blocks to pillar up (default: 3, max: 10).",
                        "default": 3,
                    },
                    "reason": {
                        "type": "string",
                        "description": "Explanation of why the bot is pillaring up.",
                    },
                },
                "required": ["height"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "auto_sleep",
            "description": "Search for a nearby bed, walk near it, and sleep through the night or thunderstorm. Will wake automatically when morning comes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "radius": {
                        "type": "integer",
                        "description": "Search radius in blocks for beds (default: 16).",
                        "default": 16,
                    },
                    "threat_check": {
                        "type": "boolean",
                        "description": "Whether to verify that no nearby hostile mobs threaten the bot before sleeping.",
                        "default": True,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chat_say",
            "description": "Send a chat message to other players in the game server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message text to broadcast in in-game chat.",
                    }
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_to",
            "description": "Navigate to specific coordinates in the world using pathfinder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "X coordinate."},
                    "y": {"type": "number", "description": "Y coordinate."},
                    "z": {"type": "number", "description": "Z coordinate."},
                },
                "required": ["x", "y", "z"],
            },
        },
    },
]
