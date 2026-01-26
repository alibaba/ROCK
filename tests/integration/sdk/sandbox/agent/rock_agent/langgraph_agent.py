import argparse

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent


@tool
def write_file(filename: str, content: str) -> str:
    """Write content to a file"""
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote to {filename}"
    except Exception as e:
        return f"Error writing to file: {str(e)}"


def create_agent():
    llm = ChatOpenAI(
        model="test-model",
        base_url="http://localhost:8080/v1",
        temperature=0,
    )
    return create_react_agent(llm, [write_file])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent tool")
    parser.add_argument("query", help="Query to execute")
    args = parser.parse_args()

    agent_executor = create_agent()
    result = agent_executor.invoke({"messages": [("user", args.query)]})
    print(result["messages"][-1].content)
