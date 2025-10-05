import datetime
from google.generativeai.protos import Tool, FunctionDeclaration, Schema
from google.generativeai.types.content_types import ContentType

def get_current_time() -> str:
    print(">>> TOOL EXECUTED: get_current_time()")
    return datetime.datetime.now().strftime("%I:%M %p")

# This is a mapping of function names to the actual functions.
# It helps us easily call the right function when the AI asks for it.
AVAILABLE_TOOLS = {
    "get_current_time": get_current_time,
}

GET_TIME_SCHEMA = Tool(
    function_declarations=[
        FunctionDeclaration(
            name="get_current_time",
            description="Use this function to get the current time.",
            # This function takes no arguments, so parameters is an empty object.
            parameters=Schema(
                type="OBJECT",
                properties={},
            )
        )
    ]
)