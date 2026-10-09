import { Client } from "@notionhq/client";

const notion = new Client({ auth: process.env.NOTION_API_KEY });
const databaseId = process.env.NOTION_DATABASE_ID;

export async function createNotionPage(title: string, content: string): Promise<void> {
  if (!notion || !databaseId) {
    console.error("Notion API key or Database ID is not set in environment variables.");
    return;
  }

  try {
    await notion.pages.create({
      parent: { database_id: databaseId },
      properties: {
        title: [
          {
            text: {
              content: title,
            },
          },
        ],
      },
      children: [
        {
          object: "block",
          type: "paragraph",
          paragraph: {
            rich_text: [
              {
                type: "text",
                text: {
                  content: content,
                },
              },
            ],
          },
        },
      ],
    });
    console.log("✅ Successfully created Notion page.");
  } catch (error) {
    console.error("❌ Error creating Notion page:", error);
  }
}
