package chat.transcript;

import chat.wire.Frame;
import chat.wire.FrameDecoder;
import chat.wire.FrameEncoder;
import chat.wire.FrameType;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.List;

public final class HiddenTranscriptTest {
    private static int failures;

    private static void check(boolean ok, String what) {
        System.out.println((ok ? "ok   " : "FAIL ") + what);
        if (!ok) failures++;
    }

    private static Transcript roundTrip(Transcript t) throws Exception {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        TranscriptWriter.write(t, buf);
        return TranscriptReader.read(new ByteArrayInputStream(buf.toByteArray()));
    }

    public static void main(String[] args) throws Exception {
        String greet = "Grüße aus München";
        String jp = "注文が届きません";
        String emoji = "thanks 😀📦";

        try {
            Transcript t = new Transcript("Ticket été")
                    .event("agent joined ✔")
                    .say("Zoë", greet)
                    .say("agent", "ok")
                    .say("田中", jp)
                    .say("customer", emoji)
                    .say("agent", "last line");
            Transcript back = roundTrip(t);
            check(back.title().equals("Ticket été"), "non-ASCII title");
            check(back.events().equals(Arrays.asList("agent joined ✔")), "non-ASCII event");
            List<Message> m = back.messages();
            check(m.equals(t.messages()), "all messages round trip");
            check(m.size() == 5 && m.get(4).text().equals("last line"), "messages after non-ASCII ones intact");
        } catch (Exception e) {
            check(false, "round trip threw " + e);
        }

        try {
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            FrameEncoder enc = new FrameEncoder(buf);
            enc.write(new Frame(FrameType.SYSTEM, "ä"));
            enc.flush();
            DataInputStream in = new DataInputStream(new ByteArrayInputStream(buf.toByteArray()));
            in.readByte();
            int len = in.readInt();
            check(len == 2 && buf.size() == 7, "length prefix counts encoded bytes");
            FrameDecoder dec = new FrameDecoder(new ByteArrayInputStream(buf.toByteArray()));
            check(dec.read().body().equals("ä") && dec.read() == null, "decoder reads one frame then ends");
        } catch (Exception e) {
            check(false, "frame test threw " + e);
        }

        try {
            Path p = Files.createTempFile("transcript", ".cht");
            Transcript t = new Transcript("plain").say("a", "hello").say("b", jp + greet);
            TranscriptWriter.save(t, p);
            Transcript back = TranscriptReader.load(p);
            Files.delete(p);
            check(back.messages().equals(t.messages()), "save/load through a file");
        } catch (Exception e) {
            check(false, "file test threw " + e);
        }

        try {
            Transcript ascii = roundTrip(new Transcript("t").say("a", "plain ascii"));
            check(ascii.messages().get(0).text().equals("plain ascii"), "ASCII still works");
            byte[] utf8 = "x".getBytes(StandardCharsets.UTF_8);
            check(utf8.length == 1, "sanity");
        } catch (Exception e) {
            check(false, "ascii test threw " + e);
        }

        if (failures > 0) {
            System.out.println(failures + " failure(s)");
            System.exit(1);
        }
    }
}
