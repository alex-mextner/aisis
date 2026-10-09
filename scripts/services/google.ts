import { promises as fs } from "node:fs";
import path from "node:path";
import process from "node:process";
import { authenticate } from "@google-cloud/local-auth";
import { google, Auth } from "googleapis";

const SCOPES = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/gmail.send"];
const CREDENTIALS_PATH = path.join(process.cwd(), "credentials.json");
const TOKEN_PATHS = {
  personal: path.join(process.cwd(), "token-personal.json"),
  work: path.join(process.cwd(), "token-work.json"),
};

type AccountType = "personal" | "work";

async function loadSavedCredentialsIfExist(accountType: AccountType): Promise<Auth.OAuth2Client | null> {
  try {
    const content = await fs.readFile(TOKEN_PATHS[accountType], "utf-8");
    const credentials = JSON.parse(content);
    return google.auth.fromJSON(credentials) as Auth.OAuth2Client;
  } catch (err) {
    return null;
  }
}

async function saveCredentials(client: Auth.OAuth2Client, accountType: AccountType): Promise<void> {
  const content = await fs.readFile(CREDENTIALS_PATH, "utf-8");
  const keys = JSON.parse(content);
  const key = keys.installed || keys.web;
  const payload = JSON.stringify({
    type: "authorized_user",
    client_id: key.client_id,
    client_secret: key.client_secret,
    refresh_token: client.credentials.refresh_token,
  });
  await fs.writeFile(TOKEN_PATHS[accountType], payload);
}

export async function authorize(accountType: AccountType): Promise<Auth.OAuth2Client> {
  let client = await loadSavedCredentialsIfExist(accountType);
  if (client) {
    return client;
  }

  const authClient = await authenticate({
    scopes: SCOPES,
    keyfilePath: CREDENTIALS_PATH,
  });

  if (!authClient || !authClient.credentials) {
    throw new Error(`Authentication failed for ${accountType}.`);
  }

  // Cast to the correct OAuth2Client type
  const oAuth2Client = new google.auth.OAuth2(
    (authClient as any)._clientId,
    (authClient as any)._clientSecret,
    (authClient as any)._redirectUri,
  );
  oAuth2Client.setCredentials(authClient.credentials);

  await saveCredentials(oAuth2Client, accountType);
  return oAuth2Client;
}

export async function createCalendarEvent(
  auth: Auth.OAuth2Client,
  summary: string,
  description: string,
  start: string,
  end: string,
) {
  const calendar = google.calendar({ version: "v3", auth });
  try {
    await calendar.events.insert({
      calendarId: "primary",
      requestBody: {
        summary,
        description,
        start: { dateTime: start, timeZone: "UTC" },
        end: { dateTime: end, timeZone: "UTC" },
      },
    });
    console.log("✅ Successfully created calendar event.");
  } catch (error) {
    console.error("❌ Error creating calendar event:", error);
  }
}

export async function sendEmail(auth: Auth.OAuth2Client, to: string, subject: string, body: string) {
  const gmail = google.gmail({ version: "v1", auth });
  const utf8Subject = `=?utf-8?B?${Buffer.from(subject).toString("base64")}?=`;
  const messageParts = [
    `To: ${to}`,
    `Subject: ${utf8Subject}`,
    "MIME-Version: 1.0",
    "Content-Type: text/html; charset=utf-8",
    "Content-Transfer-Encoding: 7bit",
    "",
    body,
  ];
  const message = messageParts.join("\n");

  try {
    await gmail.users.messages.send({
      userId: "me",
      requestBody: {
        raw: Buffer.from(message).toString("base64url"),
      },
    });
    console.log("✅ Successfully sent email.");
  } catch (error) {
    console.error("❌ Error sending email:", error);
  }
}
