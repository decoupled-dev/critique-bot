package chat.transcript;

import java.util.Objects;

/** A chat line: who said it and what they said. Stored as "author\u001ftext". */
public final class Message {
    static final char SEP = '\u001f';

    private final String author;
    private final String text;

    public Message(String author, String text) {
        this.author = Objects.requireNonNull(author);
        this.text = Objects.requireNonNull(text);
    }

    public String author() {
        return author;
    }

    public String text() {
        return text;
    }

    String encode() {
        return author + SEP + text;
    }

    static Message decode(String body) {
        int i = body.indexOf(SEP);
        if (i < 0) {
            return new Message("?", body);
        }
        return new Message(body.substring(0, i), body.substring(i + 1));
    }

    @Override
    public boolean equals(Object o) {
        if (!(o instanceof Message)) return false;
        Message m = (Message) o;
        return author.equals(m.author) && text.equals(m.text);
    }

    @Override
    public int hashCode() {
        return Objects.hash(author, text);
    }

    @Override
    public String toString() {
        return author + ": " + text;
    }
}
