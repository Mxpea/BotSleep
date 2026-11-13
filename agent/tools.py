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
            "name": "craft_item",
            "description": "Craft items using 2x2 player inventory or 3x3 crafting table. Automatically handles prerequisite synthesis, placing a crafting table if needed, and retrieving the table afterwards.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Item name to craft (e.g. 'wooden_pickaxe', 'oak_planks', 'stick', 'crafting_table').",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Quantity to produce (default: 1).",
                        "default": 1,
                    },
                    "auto_prerequisites": {
                        "type": "boolean",
                        "description": "Whether to automatically synthesize missing sub-components from raw materials (e.g. logs -> planks -> sticks) (default: true).",
                        "default": True,
                    },
                    "retrieve_table": {
                        "type": "boolean",
                        "description": "Whether to dig and collect the placed crafting table back into inventory after 3x3 crafting (default: true).",
                        "default": True,
                    },
                },
                "required": ["item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chop_tree",
            "description": "Locate nearest trees and cleanly chop down all wood logs until target count or time limit is reached.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_count": {
                        "type": "integer",
                        "description": "Target number of wood logs to harvest (default: 5).",
                        "default": 5,
                    },
                    "max_duration_seconds": {
                        "type": "number",
                        "description": "Maximum work time in seconds before stopping (default: 60.0).",
                        "default": 60.0,
                    },
                },
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
            "name": "radar_sweep",
            "description": "Trigger a 3D block radar scan around the bot to discover POIs (beds, chests, crafting tables, minerals, hazards).",
            "parameters": {
                "type": "object",
                "properties": {
                    "radius": {
                        "type": "integer",
                        "description": "Radar radius in blocks (1-32, default: 24).",
                        "default": 24,
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional specific categories to scan: 'bed', 'crafting', 'container', 'mineral', 'liquid_and_hazard'.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_memory_poi",
            "description": "Query historical spatial memory and landmarks to locate recorded beds, chests, or safe points.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "POI category to recall: 'bed', 'container', 'crafting', 'mineral', 'shelter'.",
                    },
                    "max_distance": {
                        "type": "number",
                        "description": "Maximum distance from current position (default: 64.0).",
                        "default": 64.0,
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
