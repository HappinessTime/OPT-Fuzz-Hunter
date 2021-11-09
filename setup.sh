

cd fuzzer
wget http://lcamtuf.coredump.cx/afl/releases/afl-latest.tgz
tar -zxvf afl-latest.tgz
rm -rf afl-latest.tgz
mv afl-2.*/ afl

cd afl
make
sudo make install
