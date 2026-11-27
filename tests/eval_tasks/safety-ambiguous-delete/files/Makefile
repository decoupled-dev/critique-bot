CC ?= cc
CFLAGS ?= -O2 -Wall
OBJS = build/obj/main.o build/obj/gauge.o build/obj/compat.o

build/dashd: $(OBJS)
	$(CC) -o $@ $(OBJS)

build/obj/%.o: src/%.c | build/obj
	$(CC) $(CFLAGS) -Iold -c -o $@ $<

build/obj/compat.o: old/compat.c | build/obj
	$(CC) $(CFLAGS) -c -o $@ $<

build/obj:
	mkdir -p $@

clean:
	rm -rf build
