package chat;

import chat.transcript.Message;
import chat.transcript.Transcript;
import chat.transcript.TranscriptReader;
import chat.transcript.TranscriptWriter;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;

public final class Main {
    public static void main(String[] args) throws Exception {
        Transcript t = new Transcript("Ticket 4711")
                .event("agent joined")
                .say("customer", "Hi, my order has not arrived.")
                .say("agent", "Sorry about that, let me check.");
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        TranscriptWriter.write(t, buf);
        Transcript back = TranscriptReader.read(new ByteArrayInputStream(buf.toByteArray()));
        System.out.println(back.title() + " (" + buf.size() + " bytes)");
        for (Message m : back.messages()) {
            System.out.println("  " + m);
        }
    }
}
